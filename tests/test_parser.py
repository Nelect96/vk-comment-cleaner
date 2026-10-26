import io
import unittest
import zipfile
from vkmgr.parser import parse_comment_link, parse_zip


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for path, contents in files.items():
            z.writestr(path, contents)
    return data.getvalue()


class URLParsing(unittest.TestCase):
    def test_wall(self):
        result = parse_comment_link('https://vk.com/wall-123_456?reply=789&thread=56')
        self.assertEqual(result[:4], ('wall', -123, 456, 789))

    def test_photo(self):
        result = parse_comment_link('https://vk.com/photo123_234?comment=1000')
        self.assertEqual(result[:4], ('photo', 123, 234, 1000))

    def test_video(self):
        result = parse_comment_link('https://vk.com/video-123_234?comment_id=800')
        self.assertEqual(result[:4], ('video', -123, 234, 800))

    def test_wall_w_param(self):
        result = parse_comment_link('https://vk.com/somegroup?w=wall-123_456_r789')
        self.assertEqual(result[:4], ('wall', -123, 456, 789))

    def test_unsafe_hosts(self):
        for url in ('https://vk.com.evil.org/wall-1_2?reply=3', 'javascript:alert(1)', 'file:///etc/passwd', 'https://evil.com/video1_2?reply=3'):
            self.assertIsNone(parse_comment_link(url))

    def test_no_guess_without_comment(self):
        self.assertIsNone(parse_comment_link('https://vk.com/wall-1_2'))


class ZipParsing(unittest.TestCase):
    def test_import_three_types_and_deduplicate(self):
        html = '''<html><body>
        <div class="item"><p>12.10.2026 Важный комментарий к записи.</p><a href="https://vk.com/wall-10_4?reply=22">Открыть</a></div>
        <div class="item"><p>11.10.2026 Очень красивый снимок.</p><a href="https://vk.com/photo12_34?reply=55">Перейти</a></div>
        <div class="item"><p>10.10.2026 Интересная видеозапись.</p><a href="https://vk.com/video12_34?reply=66">Перейти</a></div>
        </body></html>'''
        blob = archive({'archive/comments/comments0.html':html, 'archive/comments/comments1.html':html, 'archive/index.html':"<a href='https://vk.com/wall-1_2?reply=4'>other</a>"})
        records, meta = parse_zip(blob)
        self.assertEqual(len(records), 3)
        self.assertEqual(meta['files'], 2)
        self.assertEqual({x.kind for x in records}, {'wall','photo','video'})
        self.assertIn('Очень красивый снимок', records[1].text)
        self.assertEqual(records[0].date, '12.10.2026')

    def test_distinct_parent_items_with_same_comment_id(self):
        html = """<div class="item"><a href="https://vk.com/wall-10_4?reply=22">First</a></div>
                  <div class="item"><a href="https://vk.com/wall-10_5?reply=22">Second</a></div>"""
        records, meta = parse_zip(archive({'comments0.html': html}))
        self.assertEqual(meta['found'], 2)
        self.assertEqual({r.item_id for r in records}, {4, 5})
        self.assertEqual(len({r.id for r in records}), 2)

    def test_mixed_anchor_and_plain_text_urls(self):
        html = """<div class="item"><a href="https://vk.com/wall-10_4?reply=22">First</a></div>
                  <p>Another comment: https://vk.com/photo10_7?comment=45</p>"""
        records, meta = parse_zip(archive({'comments0.html': html}))
        self.assertEqual(meta['found'], 2)
        self.assertEqual({r.kind for r in records}, {'wall', 'photo'})

    def test_reject_bad_zip(self):
        with self.assertRaises(ValueError): parse_zip(b'not a zip')

    def test_no_extraction_or_traversal(self):
        blob = archive({'../../comments.html':'<a href="https://vk.com/wall1_2?reply=3">sample</a>'})
        comments,meta=parse_zip(blob)
        self.assertEqual(len(comments),1)

    def test_skips_unrelated(self):
        blob = archive({'comments0.html':'No matching links','user/messages.html':'<a href="https://vk.com/wall1_2?reply=3">Hi</a>'})
        comments,meta=parse_zip(blob)
        self.assertEqual(len(comments),0)



class RealVKArchiveStructure(unittest.TestCase):
    """Sanitized structure-only fixtures, no personal archive material."""

    def test_2026_cp1251_comment_row_with_quoted_link_is_single_comment(self):
        html = """
        <div class="item">
          <div class='item__main'>Мой пример текста.
            https://vk.ru/wall-55_99?reply=888 это цитата, а не мой комментарий
          </div>
          <div class='item__main'>
             <a href="https://vk.ru/wall-111_222?reply=333">Ссылка</a>
          </div>
          <div class='item__tertiary'></div>
          <div class='item__tertiary'>21 окт 2026 в 10:36</div>
        </div>"""
        blob = archive({"comments/comments.html": html.encode("cp1251")})
        comments, meta = parse_zip(blob)
        self.assertEqual(meta["files"], 1)
        self.assertEqual(meta["found"], 1)
        self.assertEqual(comments[0].id, "wall:-111:222:333")
        self.assertEqual(comments[0].date, "21 окт 2026 в 10:36")
        self.assertIn("Мой пример текста", comments[0].text)
        self.assertNotIn("это цитата", comments[0].url)

    def test_large_normal_vk_export_ignores_media_comments_and_messages(self):
        entries = {}
        for n in range(47):
            page = ("comments/comments.html" if n == 0
                    else "comments/comments{}.html".format(n + 1))
            entries[page] = (
                "<div class='item'><div class='item__main'>Тест</div>"
                "<div class='item__main'><a href='https://vk.ru/wall-111_222?reply={}'"
                ">Ссылка</a></div><div class='item__tertiary'>"
                "21 окт 2026 в 10:36</div></div>".format(n + 1000)
            )
        for n in range(3890):
            entries["messages/msg{}.html".format(n)] = "PRIVATE PLACEHOLDER"
        entries["photos/photo-comments/555/comments0.html"] = (
            "<a href='https://vk.ru/wall-5_6?reply=7'>Not authored history</a>")
        entries["wall/1234/comments0.html"] = (
            "<a href='https://vk.ru/wall-8_9?reply=10'>Not authored history</a>")
        comments, meta = parse_zip(archive(entries))
        self.assertEqual(meta["files"], 47)
        self.assertEqual(meta["found"], 47)
        self.assertEqual(comments[0].comment_id, 1000)
        self.assertEqual(comments[-1].comment_id, 1046)
        self.assertFalse(any(x.owner_id in (-5, -8) for x in comments))

    def test_read_only_zip_path_classifier_excludes_unrelated_subfolders(self):
        from vkmgr.parser import _is_comment_page
        self.assertTrue(_is_comment_page("comments/comments2.html"))
        self.assertTrue(_is_comment_page("archive/comments/comments47.html"))
        self.assertTrue(_is_comment_page("comments0.html"))
        self.assertFalse(_is_comment_page("video/video-comments/123/comments0.html"))
        self.assertFalse(_is_comment_page("photos/photo-comments/99/comments0.html"))
        self.assertFalse(_is_comment_page("wall/1123/comments0.html"))
        self.assertFalse(_is_comment_page("messages/comments0.html"))



if __name__ == '__main__': unittest.main()
