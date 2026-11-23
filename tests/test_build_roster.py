"""名簿づくりの試験（Discord にはつながない。架空のサーバーで確かめる）。

実行: python -m unittest discover -s tests
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_roster as br  # noqa: E402

WORLD = "wrld_12345678-1234-1234-1234-123456789abc"

CONFIG = {
    "plans": [
        {"plan": 1, "roles": ["Patreon Bronze", "FANBOX 松"]},
        {"plan": 2, "roles": ["Patreon Silver"]},
        {"plan": 3, "roles": ["Patreon Gold", "FANBOX 梅"]},
    ],
    "optout_role": "パネルに載せない",
    "photo_slots": 10,
}

ROLES = [
    {"id": "r1", "name": "Patreon Bronze"},
    {"id": "r2", "name": "Patreon Silver"},
    {"id": "r3", "name": "Patreon Gold"},
    {"id": "f1", "name": "FANBOX 松"},
    {"id": "f3", "name": "FANBOX 梅"},
    {"id": "op", "name": "パネルに載せない"},
    {"id": "xx", "name": "一般"},
]


def member(uid, nick, roles, bot=False):
    return {"user": {"id": uid, "bot": bot}, "nick": nick, "roles": roles}


class BuildTest(unittest.TestCase):
    def build(self, members, overrides=None, config=CONFIG):
        return br.build_members({"roles": ROLES, "members": members}, config,
                                {"members": overrides or {}})

    def test_plans_optout_and_skips(self):
        out, stats = self.build([
            member("1", "Alice", ["r1"]),
            member("2", " Bob ", ["r1", "r3"]),          # 高い方のプラン・前後の空白は除く
            member("3", "ひみつさん", ["f1", "op"]),        # 載せない → ハッシュ
            member("4", None, ["r2"]),                     # ニックネーム無し → 載せない
            member("5", "Carol", ["xx"]),                  # 契約していない
            member("6", "BotName", ["r3"], bot=True),      # Bot は除く
        ])
        self.assertEqual(out[0], {"name": "Bob", "plan": 3})
        self.assertEqual(out[1], {"name": "Alice", "plan": 1})
        self.assertEqual(out[2], {"hash": br.name_hash("ひみつさん"), "plan": 1})
        self.assertEqual(len(out), 3)
        self.assertEqual(stats["no_nick"], 1)
        self.assertEqual((stats["panel"], stats["hidden"]), (2, 1))
        self.assertNotIn("ひみつさん", json.dumps(out, ensure_ascii=False))

    def test_hash_rule(self):
        # 前後の空白だけ除いて SHA-256（大文字小文字は区別する）
        expected = hashlib.sha256("GinGira".encode("utf-8")).hexdigest()
        self.assertEqual(br.name_hash("  GinGira "), expected)
        self.assertNotEqual(br.name_hash("gingira"), expected)

    def test_overrides(self):
        out, stats = self.build(
            [member("1", "Alice", ["r3"]), member("2", "Bob", ["r3"])],
            {
                "1": {"photo_slot": 2, "portal": WORLD},
                "2": {"photo_slot": 2, "portal": "wrld_bad"},   # 枠の重なり・ID の誤り
                "9": {"photo_slot": 3},                          # 契約していない人
            },
        )
        self.assertEqual(out[0], {"name": "Alice", "plan": 3, "photo": 2, "portal": WORLD})
        self.assertEqual(out[1], {"name": "Bob", "plan": 3})
        self.assertEqual(stats["unused_overrides"], 1)
        self.assertEqual(len(stats["warnings"]), 2)

    def test_duplicate_names(self):
        out, stats = self.build([
            member("1", "Alice", ["r1"]),
            member("2", "Alice ", ["r2", "op"]),
            member("3", "ALICE", ["r1"]),        # 大文字小文字が違えば別人
        ])
        out = [e for e in out if e.get("name") != "ALICE"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["plan"], 2)
        self.assertIn("hash", out[0])      # どちらかが「載せない」なら隠す側に倒す
        self.assertEqual(stats["duplicates"], 1)

    def test_missing_role_names(self):
        bad = dict(CONFIG, plans=[{"plan": 1, "roles": ["（Patreon のプラン 1 のロール名）"]}])
        with self.assertRaises(br.RosterError) as cm:
            self.build([], config=bad)
        self.assertIn("サーバーに無いロール名", str(cm.exception))

    def test_change_detection(self):
        members = [{"name": "Alice", "plan": 1}]
        first, changed = br.decide_roster(None, members)            # 初回（公開中の物が無い）
        self.assertTrue(changed)
        again, changed = br.decide_roster(first, members)           # 同じ中身
        self.assertFalse(changed)
        self.assertEqual(again["updated"], first["updated"])        # 日時も前のまま
        _, changed = br.decide_roster(first, members + [{"name": "Bob", "plan": 2}])
        self.assertTrue(changed)

    def test_main_with_sample_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
            (d / "overrides.json").write_text(json.dumps({"members": {"1": {"photo_slot": 1}}}), encoding="utf-8")
            (d / "guild.json").write_text(json.dumps(
                {"roles": ROLES, "members": [member("1", "Alice", ["r3"])]}, ensure_ascii=False),
                encoding="utf-8")
            args = ["--config", str(d / "config.json"), "--overrides", str(d / "overrides.json"),
                    "--out", str(d / "site" / "roster.json"), "--photos", str(d / "photos"),
                    "--from-file", str(d / "guild.json"), "--previous-file", str(d / "prev.json")]
            out_env = d / "gh_output.txt"
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(out_env)
            try:
                self.assertEqual(br.main(args), 0)                  # 初回 → 変わった
                roster = json.loads((d / "site" / "roster.json").read_text(encoding="utf-8"))
                self.assertEqual(roster["members"], [{"name": "Alice", "plan": 3, "photo": 1}])
                (d / "prev.json").write_text(json.dumps(roster), encoding="utf-8")
                self.assertEqual(br.main(args), 0)                  # 2 回目 → 変わらない
            finally:
                if old is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old
            self.assertEqual(out_env.read_text(encoding="utf-8").split(), ["changed=true", "changed=false"])
            self.assertEqual(br.missing_photo_files(roster["members"], d / "photos"), [1])

    def test_real_config_is_placeholder(self):
        # 実物の config.json はロール名がまだ仮なので、はっきりエラーになること
        config = json.loads((br.ROOT / "config.json").read_text(encoding="utf-8"))
        with self.assertRaises(br.RosterError):
            self.build([], config=config)


if __name__ == "__main__":
    unittest.main()
