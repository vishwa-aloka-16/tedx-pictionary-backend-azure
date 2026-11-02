import io
import json
import unittest
from unittest.mock import Mock, patch

from backend.artwork import ArtworkStore
from backend.artwork_publisher import ArtworkPublisher


class ArtworkPublisherTests(unittest.TestCase):
    def test_warm_up_pings_configured_health_url_without_upload_credentials(self):
        with patch('backend.artwork_publisher.urllib.request.urlopen', return_value=io.BytesIO(b'{"status":"ok"}')) as send:
            self.assertTrue(ArtworkPublisher('https://art.example.com', 'x' * 32).warm_up())
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://art.example.com/health')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(send.call_args.kwargs['timeout'], 60)

    @patch('backend.artwork.threading.Thread.start')
    def test_warm_up_runs_in_background_and_deduplicates_concurrent_requests(self, start):
        publisher = Mock()
        store = ArtworkStore(publisher=publisher)
        store.warm_up()
        store.warm_up()
        start.assert_called_once()
        publisher.warm_up.assert_not_called()
        store._warm_up()
        publisher.warm_up.assert_called_once()
        self.assertFalse(store.warming_up)
        store.warm_up()
        self.assertEqual(start.call_count, 2)

    @patch('backend.artwork.threading.Thread.start')
    def test_failed_warm_up_does_not_escape_or_prevent_future_attempts(self, _start):
        store = ArtworkStore(publisher=Mock())
        store.publisher.warm_up.side_effect = OSError('offline')
        store.warm_up()
        store._warm_up()
        self.assertFalse(store.warming_up)

    def test_upload_uses_configured_domain_and_private_key(self):
        token = 'random-artwork-token-123456'
        url = f'https://art.example.com/images/{token}'
        response = io.BytesIO(json.dumps({'id': token, 'url': url}).encode())
        with patch('backend.artwork_publisher.urllib.request.urlopen', return_value=response) as send:
            result = ArtworkPublisher('https://art.example.com/', 'x' * 32)(token, b'PNG')
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.method, 'PUT')
        self.assertEqual(request.data, b'PNG')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + 'x' * 32)
        self.assertEqual(result, url)

    def test_incorrect_service_domain_does_not_create_a_localhost_qr(self):
        response = io.BytesIO(json.dumps({'id': 'token', 'url': 'http://localhost:3001/images/token'}).encode())
        with patch('backend.artwork_publisher.urllib.request.urlopen', return_value=response):
            with self.assertRaises(ValueError):
                ArtworkPublisher('https://art.example.com', 'x' * 32)('token', b'PNG')

    def test_invalid_service_configuration_is_rejected(self):
        for url in ['ftp://art.example.com', 'https://art.example.com/images', 'https://user:key@art.example.com']:
            with self.assertRaises(ValueError):
                ArtworkPublisher(url, 'x' * 32)
        with self.assertRaises(ValueError):
            ArtworkPublisher('https://art.example.com', '')

    @patch('backend.artwork.threading.Thread.start')
    def test_finished_artwork_starts_background_upload_and_exposes_url_only_after_success(self, start):
        publisher = Mock(return_value='https://art.example.com/images/token')
        store = ArtworkStore(publisher=publisher)
        store.render = Mock(return_value=b'PNG')
        store.capture('token', ['One', 'Two'], [], complete=False)
        start.assert_not_called()
        store.capture('token', ['One', 'Two'], [], complete=True)
        start.assert_called_once()
        self.assertIsNone(store.info('token')['share_url'])
        store._publish('token', store.records['token'])
        self.assertEqual(store.info('token')['share_url'], 'https://art.example.com/images/token')
        publisher.assert_called_once_with('token', b'PNG')
        self.assertTrue(store.info('token')['sharing_enabled'])
        start.assert_called_once()

    @patch('backend.artwork.threading.Thread.start')
    def test_failed_upload_retries_without_stopping_the_game(self, start):
        publisher = Mock(side_effect=OSError('offline'))
        store = ArtworkStore(publisher=publisher)
        store.render = Mock(return_value=b'PNG')
        with patch('backend.artwork.time.time', return_value=100):
            store.capture('token', ['One', 'Two'], [], complete=True)
            with self.assertLogs('backend.artwork', level='WARNING'):
                store._publish('token', store.records['token'])
            self.assertIsNone(store.info('token')['share_url'])
            self.assertEqual(store.info('token')['share_status'], 'retrying')
            start.assert_called_once()
        with patch('backend.artwork.time.time', return_value=106):
            store.info('token')
            self.assertEqual(start.call_count, 2)

    @patch('backend.artwork.threading.Thread.start')
    def test_late_canvases_do_not_publish_a_stale_version(self, _start):
        store = ArtworkStore(publisher=Mock())
        store.render = Mock(return_value=b'PNG')
        def publish(_token, _png):
            store.attach('token', 'round', 'one', b'new-image')
            return 'https://art.example.com/images/token'
        store.publisher = publish
        store.capture('token', ['One', 'Two'], [], complete=True)
        store._publish('token', store.records['token'])
        self.assertIsNone(store.records['token'].get('share_url'))

    @patch('backend.artwork.threading.Thread.start')
    def test_backend_qr_uses_hosted_url_and_waits_for_upload(self, _start):
        import importlib
        from fastapi import HTTPException
        module = importlib.import_module('backend.app')
        info = {'sharing_enabled': True, 'share_url': None}
        with patch.object(module.game_manager.artworks, 'info', return_value=info):
            with self.assertRaises(HTTPException) as error:
                module.artwork_qr('token', frontend_origin=None)
            self.assertEqual(error.exception.status_code, 409)
            info['share_url'] = 'https://art.example.com/images/token'
            with patch('backend.app.qrcode.QRCode') as qr:
                module.artwork_qr('token', frontend_origin=None)
                qr.return_value.add_data.assert_called_once_with(info['share_url'])
