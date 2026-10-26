import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from vkmgr.parser import Comment
from vkmgr.vk_api import action, VkApiError


class Stub:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self): return io.BytesIO(json.dumps(self.payload).encode())
    def __exit__(self, *args): pass


class APIActions(unittest.TestCase):
    def test_method_routing_and_request_body(self):
        for kind, method in [('wall','wall.deleteComment'),('photo','photos.deleteComment'),('video','video.deleteComment')]:
            c=Comment(f'{kind}:1:3',kind,1,2,3,'https://vk.com/','','','comments0.html')
            calls=[]
            def fake(req, timeout):
                calls.append(req)
                return Stub({'response': 1})
            with patch('vkmgr.vk_api.urlopen', side_effect=fake):
                self.assertTrue(action('private-token',c,'delete'))
            self.assertEqual(len(calls),1)
            req=calls[0]
            self.assertEqual(req.full_url, 'https://api.vk.com/method/'+method)
            params=parse_qs(req.data.decode())
            self.assertEqual(params['comment_id'], ['3'])
            self.assertNotIn('access_token', params)
            self.assertEqual(req.get_header('Authorization'), 'Bearer private-token')

    def test_edit_params(self):
        c=Comment('video:1:3','video',1,2,3,'','','','')
        with patch('vkmgr.vk_api.urlopen', return_value=Stub({'response': 1})) as mock:
            action('token',c,'edit','новый текст')
            params=parse_qs(mock.call_args.args[0].data.decode())
            self.assertEqual(params['message'], ['новый текст'])
            self.assertIn('video.editComment',mock.call_args.args[0].full_url)

    def test_false_success_is_error(self):
        c=Comment('wall:1:3','wall',1,2,3,'','','','')
        with patch('vkmgr.vk_api.urlopen',return_value=Stub({'response':0})):
            with self.assertRaises(VkApiError):action('token',c,'delete')

    def test_vk_error(self):
        c=Comment('wall:1:3','wall',1,2,3,'','','','')
        with patch('vkmgr.vk_api.urlopen',return_value=Stub({'error':{'error_code':1051,'error_msg':'not available'}})):
            with self.assertRaises(VkApiError) as error:action('token',c,'delete')
            self.assertEqual(error.exception.code,1051)

if __name__ == '__main__':unittest.main()
