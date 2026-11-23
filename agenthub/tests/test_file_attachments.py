"""Upload/download policy and persisted file paths for channel and job reads."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
import mcp_bridge
from jobs import JobStore
from store import MessageStore


class FileAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.uploads = self.root / 'uploads'
        self.config = {'server': {'port': 8300}, 'images': {'upload_dir': str(self.uploads)},
                       'attachments': {'max_size_mb': 1}}
        api = FastAPI()
        api.add_api_route('/api/upload', app.upload_image, methods=['POST'])
        api.add_api_route('/uploads/{filename}', app.serve_upload, methods=['GET'])
        for name, value in {'app': api, 'config': self.config, 'registry': None,
                            'session_token': 'attachment-test'}.items():
            self.stack.enter_context(patch.object(app, name, value))
        app._install_security_middleware('attachment-test', self.config)
        self.client = self.stack.enter_context(TestClient(api, base_url="http://127.0.0.1"))
        self.headers = {'X-Session-Token': 'attachment-test'}

    def upload(self, name, content=b'test bytes', mime='application/octet-stream'):
        return self.client.post('/api/upload', files={'file': (name, content, mime)},
                                headers=self.headers)

    def test_documents_round_trip_with_metadata_and_authenticated_download(self):
        for extension in ['pdf', 'docx', 'xlsx', 'csv', 'txt', 'md', 'log', 'zip', 'py', 'wav', 'exe', 'html', 'unusual', 'c++', '日本語']:
            with self.subTest(extension=extension):
                data = ('unique content: ' + extension).encode()
                response = self.upload('report.' + extension, data)
                self.assertEqual(response.status_code, 200)
                attachment = response.json()
                self.assertEqual(attachment['kind'], 'file')
                self.assertEqual(attachment['size'], len(data))
                self.assertEqual(attachment['name'], 'report.' + extension)
                self.assertTrue(attachment['media_type'])
                self.assertEqual(self.client.get(attachment['url']).status_code, 403)
                download = self.client.get(attachment['url'], headers=self.headers)
                self.assertEqual(download.content, data)
                self.assertTrue(download.headers['content-disposition'].startswith('attachment;'))
                self.assertEqual(download.headers['x-content-type-options'], 'nosniff')

    def test_legacy_images_still_have_public_inline_previews(self):
        response = self.upload('picture.png', b'png bytes', 'image/png')
        self.assertEqual(response.json()['kind'], 'image')
        download = self.client.get(response.json()['url'])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers['content-type'], 'image/png')
        self.assertNotIn('content-disposition', download.headers)

    def test_rejections_leave_no_files_and_limits_apply_before_full_copy(self):
        self.config['attachments']['allowed_extensions'] = ['.pdf']
        self.assertEqual(self.upload('script.exe').status_code, 400)
        self.assertEqual(self.upload('page.html').status_code, 400)
        self.assertEqual(self.upload('large.pdf', b'x' * (1024 * 1024 + 1)).status_code, 413)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(self.upload('exact.pdf', b'x' * (1024 * 1024)).status_code, 200)

    def test_files_without_extensions_and_dotfiles_are_accepted(self):
        for name in ['Makefile', '.gitignore']:
            with self.subTest(name=name):
                response = self.upload(name, b'plain text')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['kind'], 'file')
                self.assertEqual(response.json()['name'], name)
                downloaded = self.client.get(response.json()['url'], headers=self.headers)
                self.assertEqual(downloaded.content, b'plain text')
                self.assertTrue(downloaded.headers['content-disposition'].startswith('attachment;'))

    def test_configurable_extensions_do_not_make_documents_executable(self):
        self.config['attachments']['allowed_extensions'] = ['.html']
        self.assertEqual(self.upload('report.pdf').status_code, 400)
        uploaded = self.upload('page.html', b'<script>alert(1)</script>').json()
        response = self.client.get(uploaded['url'], headers=self.headers)
        self.assertEqual(response.headers['content-type'], 'application/octet-stream')
        self.assertTrue(response.headers['content-disposition'].startswith('attachment;'))

    def test_names_cannot_choose_storage_paths_or_overwrite_files(self):
        one = self.upload('../../report.pdf', b'first').json()
        two = self.upload('../../report.pdf', b'second').json()
        self.assertEqual(one['name'], 'report.pdf')
        self.assertNotEqual(one['url'], two['url'])
        self.assertEqual(self.client.get(one['url'], headers=self.headers).content, b'first')
        outside = self.root / 'private.pdf'
        outside.write_bytes(b'secret')
        (self.uploads / 'link.pdf').symlink_to(outside)
        self.assertEqual(self.client.get('/uploads/link.pdf', headers=self.headers).status_code, 400)

    def test_browser_display_name_preserves_quotes_and_unicode(self):
        name = 'report " café.pdf'
        response = self.client.post('/api/upload',
                                    files={'file': ('report %22 café.pdf', b'contents')},
                                    data={'display_name': name}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['name'], name)

    def test_upload_requires_session_and_rejects_cross_origin(self):
        response = self.client.post('/api/upload', files={'file': ('x.pdf', b'x')})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/upload', files={'file': ('x.pdf', b'x')},
                                    headers={**self.headers, 'Origin': 'https://outside.example'})
        self.assertEqual(response.status_code, 403)

    def test_reloaded_channel_and_job_reads_include_original_bytes_path(self):
        attachment = self.upload('report.pdf', b'persisted file').json()
        store_path, jobs_path = self.root / 'chat.jsonl', self.root / 'jobs.json'
        messages, jobs = MessageStore(str(store_path)), JobStore(str(jobs_path))
        messages.add('human', 'read this', attachments=[attachment])
        job = jobs.create('Files', 'task', 'general', 'human')
        jobs.add_message(job['id'], 'human', 'read this too', attachments=[attachment])
        jobs.update_status(job['id'], status='archived')
        jobs = JobStore(str(jobs_path))
        jobs.update_status(job['id'], status='open')
        for name, value in {'store': MessageStore(str(store_path)), 'jobs': jobs,
                            'config': self.config, 'registry': None}.items():
            self.stack.enter_context(patch.object(mcp_bridge, name, value))
        for kwargs in ({'channel': 'general'}, {'job_id': job['id']}):
            entries = json.loads(mcp_bridge.chat_read(**kwargs))
            read = next(entry for entry in entries if entry.get('attachments'))['attachments'][0]
            self.assertEqual(read['size'], len(b'persisted file'))
            self.assertEqual(Path(read['file_path']).read_bytes(), b'persisted file')


if __name__ == '__main__':
    unittest.main()


class AgentSendAttachmentTests(unittest.TestCase):
    """Agents upload with their Bearer token and attach the result to /api/send."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.uploads = root / 'uploads'
        config = {'server': {'port': 8300}, 'images': {'upload_dir': str(self.uploads)},
                  'attachments': {'max_size_mb': 1}}
        self.store = MessageStore(str(root / 'log.jsonl'))

        class Registry:
            def resolve_token(self, token):
                return {'name': 'bot'} if token == 'agent-token' else None

        api = FastAPI()
        api.add_api_route('/api/upload', app.upload_image, methods=['POST'])
        api.add_api_route('/api/send', app.api_send, methods=['POST'])
        for name, value in {'app': api, 'config': config, 'registry': Registry(), 'store': self.store,
                            'session_token': 'session'}.items():
            self.stack.enter_context(patch.object(app, name, value))
        app._install_security_middleware('session', config)
        self.client = self.stack.enter_context(TestClient(api, base_url="http://127.0.0.1"))
        self.auth = {'Authorization': 'Bearer agent-token'}

    def test_agent_uploads_and_attaches_a_file(self):
        up = self.client.post('/api/upload', files={'file': ('005.md', b'# five', 'text/markdown')}, headers=self.auth)
        self.assertEqual(up.status_code, 200)
        sent = self.client.post('/api/send', json={'text': 'here', 'channel': 'general',
                                                   'attachments': [up.json()]}, headers=self.auth)
        self.assertEqual(sent.status_code, 200)
        msg = self.store.get_recent(1, channel='general')[-1]
        self.assertEqual(msg['sender'], 'bot')
        self.assertEqual(msg['attachments'][0]['name'], '005.md')
        self.assertEqual(msg['attachments'][0]['url'], up.json()['url'])

    def test_attachment_only_message_and_rejections(self):
        up = self.client.post('/api/upload', files={'file': ('a.png', b'png', 'image/png')}, headers=self.auth)
        self.assertEqual(self.client.post('/api/send', json={'attachments': [up.json()]}, headers=self.auth).status_code, 200)
        for bad in [{'url': '/uploads/missing.png'}, {'url': '/etc/passwd'}, {'url': '/uploads/../log.jsonl'}, 'x']:
            with self.subTest(bad=bad):
                r = self.client.post('/api/send', json={'text': 't', 'attachments': [bad]}, headers=self.auth)
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post('/api/upload', files={'file': ('a.md', b'x', 'text/plain')},
                                          headers={'Authorization': 'Bearer wrong'}).status_code, 403)
