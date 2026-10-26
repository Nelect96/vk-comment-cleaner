import unittest
from unittest.mock import patch
from vkmgr.parser import Comment
from vkmgr.vk_api import VkApiError
from vkmgr.verification import authorized_user, verify_comment


def comment(kind, item=40, cid=50):
    return Comment(f"{kind}:-123:{item}:{cid}", kind, -123, item, cid,
                   "https://vk.com/", "", "", "comments0.html")


class IdentityCheck(unittest.TestCase):
    def test_user_id_fetched_from_vk(self):
        with patch('vkmgr.verification.call_api', return_value=[{'id': 481, 'first_name': 'А', 'last_name': 'Б'}]) as api:
            self.assertEqual(authorized_user('token'), {'id': 481, 'name': 'А Б'})
            self.assertEqual(api.call_args.args[1], 'users.get')

    def test_invalid_identity_is_rejected(self):
        for data in ([], [{'id': '481'}], [{'id': 0}], {'id': 481}, None):
            with self.subTest(data=data):
                with patch('vkmgr.verification.call_api', return_value=data):
                    with self.assertRaises(VkApiError):
                        authorized_user('token')


class OwnershipCheck(unittest.TestCase):
    def test_wall_verified_only_for_exact_author_and_id(self):
        with patch('vkmgr.verification.call_api', return_value={'items': [{'id': 50, 'from_id': 481, 'post_id': 40}]}) as api:
            self.assertEqual(verify_comment('token', comment('wall'), 481).status, 'verified')
            self.assertEqual(api.call_args.args[1], 'wall.getComment')
        with patch('vkmgr.verification.call_api', return_value={'items': [{'id': 50, 'from_id': 999}]}):
            self.assertEqual(verify_comment('token', comment('wall'), 481).status, 'not_mine')
        with patch('vkmgr.verification.call_api', return_value={'items': [{'id': 51, 'from_id': 481}]}):
            self.assertEqual(verify_comment('token', comment('wall'), 481).status, 'unverified')
        with patch('vkmgr.verification.call_api', return_value={'items': [{'id': 50, 'from_id': 481, 'post_id': 42}]}):
            self.assertEqual(verify_comment('token', comment('wall'), 481).status, 'unverified')

    def test_media_uses_specific_photo_video_methods(self):
        for kind, method, item_field in [('photo', 'photos.getComments', 'photo_id'), ('video', 'video.getComments', 'video_id')]:
            with self.subTest(kind=kind):
                with patch('vkmgr.verification.call_api', return_value={'items': [{'id': 50, 'from_id': 481}]}) as api:
                    self.assertEqual(verify_comment('token', comment(kind), 481).status, 'verified')
                    self.assertEqual(api.call_args.args[1], method)
                    self.assertEqual(api.call_args.args[2][item_field], 40)
                with patch('vkmgr.verification.call_api', return_value={'items': [{'id': 50, 'from_id': 999}]}):
                    self.assertEqual(verify_comment('token', comment(kind), 481).status, 'not_mine')

    def test_media_missing_or_nested_never_assumed_deleted(self):
        with patch('vkmgr.verification.call_api', return_value={'items': []}):
            self.assertEqual(verify_comment('token', comment('video'), 481).status, 'unverified')
        with patch('vkmgr.verification.call_api', return_value={'invalid': True}):
            self.assertEqual(verify_comment('token', comment('photo'), 481).status, 'unverified')

    def test_media_pagination(self):
        with patch('vkmgr.verification.call_api', side_effect=[
            {'items': [{'id': i, 'from_id': 123} for i in range(1000, 1100)]},
            {'items': [{'id': 50, 'from_id': 481}]},
        ]) as api:
            self.assertEqual(verify_comment('token', comment('photo'), 481).status, 'verified')
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args[2]['offset'], 100)

    def test_missing_owner_id_never_verified(self):
        with patch('vkmgr.verification.call_api') as api:
            self.assertEqual(verify_comment('token', comment('wall'), 0).status, 'unverified')
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
