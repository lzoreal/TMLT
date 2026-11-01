"""Archive year page parsing: behavior tests on verbatim site markup.

Run: python -m unittest test_generate -v

夹具是从 thisamericanlife.org 归档页真实取回的 <header> 行，不是手写的近似形状：
别名行（href 不带集号）就是被旧实现整行丢弃的那一类。
"""

import unittest

import generate


# 真实 DOM：2026 年页 #885（href 只有别名）与 #899（href 带号），逐字取回。
ALIAS_ROW = """<header class="clearfix">
      <a href="/bless-this-mess" class="thumbnail goto goto-episode">
        <figure class="episode-image"><img src="https://www.thisamericanlife.org/sites/default/files/styles/archive/public/episodes/images/tal-885-getty-514697264_0.jpg?itok=3SmvcZ_s" width="356" height="200" alt=""></figure>      </a>
      <div class="container">
        <div class="field field-name-field-episode-number field-type-number-integer field-label-hidden"><div class="field-items"><div class="field-item even"><a href="/bless-this-mess" class="goto goto-episode">885</a></div></div></div>        <div class="field field-name-field-radio-air-date field-type-date field-label-hidden"><div class="field-items"><div class="field-item even"><a href="/bless-this-mess" class="goto goto-episode"><span class="date-display-single">Apr. 10, 2026</span></a></div></div></div>        <h2><a href="/bless-this-mess" class="goto goto-episode">Bless This Mess</a></h2>
      </div>
    </header>"""

NUMBERED_ROW = """<header class="clearfix">
      <a href="/899/reaching-out" class="thumbnail goto goto-episode">
        <figure class="episode-image"><img src="https://www.thisamericanlife.org/sites/default/files/styles/archive/public/episodes/images/tal-899-reachingout-sayrequevedo.jpg?itok=TOSjbYy7" width="356" height="200" alt=""></figure>      </a>
      <div class="container">
        <div class="field field-name-field-episode-number field-type-number-integer field-label-hidden"><div class="field-items"><div class="field-item even"><a href="/899/reaching-out" class="goto goto-episode">899</a></div></div></div>        <div class="field field-name-field-radio-air-date field-type-date field-label-hidden"><div class="field-items"><div class="field-item even"><a href="/899/reaching-out" class="goto goto-episode"><span class="date-display-single">Oct. 2, 2026</span></a></div></div></div>        <h2><a href="/899/reaching-out" class="goto goto-episode">Reaching Out</a></h2>
      </div>
    </header>"""

# 真实 DOM：1997 年页 #79，另有贡献者链接（同页非集行）。
OLD_ALIAS_ROW = """<header class="clearfix"> <a href="/stuck-in-the-wrong-decade" class="thumbnail goto goto-episode"> <figure class="episode-image"><img src="https://www.thisamericanlife.org/sites/default/files/styles/archive/public/episodes/images/79.jpg?itok=ZZuBKp3L" width="123" height="200" alt=""></figure> </a> <div class="container"> <div class="field field-name-field-episode-number field-type-number-integer field-label-hidden"><div class="field-items"><div class="field-item even"><a href="/stuck-in-the-wrong-decade" class="goto goto-episode">79</a></div></div></div> <div class="field field-name-field-radio-air-date field-type-date field-label-hidden"><div class="field-items"><div class="field-item even"><a href="/stuck-in-the-wrong-decade" class="goto goto-episode"><span class="date-display-single">Oct. 10, 1997</span></a></div></div></div> <h2><a href="/stuck-in-the-wrong-decade" class="goto goto-episode">Stuck in the Wrong Decade</a></h2> </div> </header>"""

CONTRIBUTOR_LINK = '<a href="/archive?year=1997&amp;contributor=8728" class="contributor">Brian Gilmore</a>'


def page(*blocks):
    return "<html><body>" + "".join(blocks) + "</body></html>"


class ParseArchiveYearTest(unittest.TestCase):
    def test_numbered_href_still_lands_in_the_roster(self):
        found = generate.parse_archive_year(page(NUMBERED_ROW))
        self.assertEqual(
            found,
            {"899": generate.BASE + "/899/reaching-out"},
        )

    def test_alias_href_row_keeps_its_number_and_its_own_url(self):
        # 旧实现：^/(\d+)/ 匹配不上就整行丢弃，885 因此从不进名册。
        found = generate.parse_archive_year(page(ALIAS_ROW))
        self.assertEqual(
            found,
            {"885": generate.BASE + "/bless-this-mess"},
        )

    def test_a_nineteen_ninety_seven_alias_row_is_not_special(self):
        found = generate.parse_archive_year(page(OLD_ALIAS_ROW, CONTRIBUTOR_LINK))
        self.assertEqual(
            found,
            {"79": generate.BASE + "/stuck-in-the-wrong-decade"},
        )

    def test_one_row_is_one_episode_not_four(self):
        # 每行渲染 4 个 a.goto-episode（缩略图/号/日期/标题），
        # 逐个 anchor 采集会把同一集记 4 次，日期与标题还会造出假条目。
        found = generate.parse_archive_year(page(ALIAS_ROW, NUMBERED_ROW, OLD_ALIAS_ROW))
        self.assertEqual(set(found), {"79", "885", "899"})

    def test_a_row_without_a_number_is_not_guessed(self):
        # 年页上确实有非集行的 header；号字段不是纯数字就不认，别拿标题里的数字凑。
        broken = ALIAS_ROW.replace(">885</a>", ">Coming soon</a>")
        self.assertEqual(generate.parse_archive_year(page(broken)), {})


if __name__ == "__main__":
    unittest.main()
