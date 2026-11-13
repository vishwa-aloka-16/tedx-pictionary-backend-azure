import asyncio
import base64
import io
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from PIL import Image, ImageDraw

from backend import app as api
from backend import model_registry
from backend import tensorflow_model_service as tensorflow_50
from backend.schemas import DrawingRequest


class SingleModelTests(unittest.TestCase):
    def test_backend_import_does_not_require_or_load_pytorch(self):
        script = '''
import importlib.abc
import sys
class NoPyTorch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "torch" or fullname.startswith("torch."):
            raise AssertionError("Backend attempted to import PyTorch")
sys.meta_path.insert(0, NoPyTorch())
from backend.app import app, health, admin_settings, game_manager
game_manager.ensure_admin_settings = lambda: None
assert health()["model"]["number_of_classes"] == 50
assert [model["key"] for model in admin_settings()["models"]] == ["tensorflow_50"]
assert not any(name == "torch" or name.startswith("torch.") for name in sys.modules)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_tensorflow_model_is_available_and_legacy_key_is_rejected(self):
        models = model_registry.get_available_models()
        self.assertEqual([model['key'] for model in models], ['tensorflow_50'])
        self.assertEqual(models[0]['number_of_classes'], 50)
        with self.assertRaises(HTTPException) as error:
            model_registry.predict_drawing('unused', 'pytorch_20')
        self.assertEqual(error.exception.status_code, 400)

    def test_standalone_endpoint_routes_to_the_50_class_model(self):
        result = {'prediction': 'cat', 'confidence': .8, 'model_key': 'tensorflow_50'}
        with patch.object(api, 'predict_drawing', return_value=result) as predict:
            response = asyncio.run(api.predict(DrawingRequest(image_data_url='test-image')))
        predict.assert_called_once_with('test-image', 'tensorflow_50')
        self.assertEqual(response, result)

    def test_health_reports_matching_model_classes_and_input_format(self):
        health = api.health()['model']
        self.assertEqual(health['classes'], tensorflow_50.classes)
        self.assertEqual(health['model_key'], 'tensorflow_50')
        self.assertEqual(health['pixel_format'], 'black strokes on white background')

    def test_existing_tensorflow_preprocessing_preserves_input_contract(self):
        canvas = Image.new('RGB', (700, 560), 'white')
        ImageDraw.Draw(canvas).line((120, 100, 500, 420), fill='black', width=15)
        output = io.BytesIO()
        canvas.save(output, format='PNG')
        prepared = tensorflow_50.prepare_canvas('data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
        self.assertEqual(prepared.shape, (1, 28, 28, 1))
        self.assertEqual(str(prepared.dtype), 'float32')
        self.assertGreaterEqual(float(prepared.min()), 0)
        self.assertLessEqual(float(prepared.max()), 1)
        self.assertGreater(float(prepared.max()), float(prepared.min()))

    def test_registry_keeps_prediction_response_shape(self):
        predict = Mock(return_value={'prediction': 'cat', 'confidence': .9, 'inference_ms': 2.5,
                                     'top_predictions': [{'prediction': 'cat', 'confidence': .9}]})
        with patch.dict(model_registry.MODEL_SERVICES['tensorflow_50'], {'predict': predict}):
            response = model_registry.predict_drawing('test-image', 'tensorflow_50')
        self.assertEqual(response['model_key'], 'tensorflow_50')
        self.assertEqual(response['prediction'], 'cat')
        self.assertEqual(response['top_predictions'][0]['confidence'], .9)
