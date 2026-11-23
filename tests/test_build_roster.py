"""名簿づくりの試験（Discord にはつながない。架空のサーバーと架空の非公開データで確かめる）。

実行: python -X utf8 -m unittest discover -s tests
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

try:
    from PIL import Image
except ImportError:  # 写真の試験だけ飛ばす
    Image = None

WORLD = "wrld_12345678-1234-1234-1234-123456789abc"
SECRET = "0123456789abcdef0123456789abcdef"
SLOTS = [f"slotname{i:02d}aaaaaaaaaaaa" for i in range(1, 11)]

CONFIG = {
    "plans": [
        {"plan": 1, "roles": ["Patreon Bronze", "FANBOX 松"]},
        {"plan": 2, "roles": ["Patreon Silver"]},
        {"plan": 3, "roles": ["Patreon Gold", "FANBOX 梅"]},
    ],
    "optout_role": "パネルに載せない",
    "photo_slots": 10,
    "photo_max_px": 256,
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


def make_data(d: Path, overrides: dict, photos=("p.png",)) -> Path:
    data = d / "data"
    (data / "photos").mkdir(parents=True)
    (data / "secret.json").write_text(json.dumps({"link_secret": SECRET, "slot_names": SLOTS}), encoding="utf-8")
    (data / "overrides.json").write_text(json.dumps({"members": overrides}), encoding="utf-8")
    for name in photos:
        if Image is not None:
            Image.new("RGB", (1200, 800), (200, 50, 50)).save(data / "photos" / name)
        else:
            (data / "photos" / name).write_bytes(b"x")
    return data


class BuildTest(unittest.TestCase):
    def build(self, members, data=None, config=CONFIG):
        data = data or {"overrides": {}, "secret": None, "slot_names": [], "photos_dir": None}
        return br.build_members({"roles": ROLES, "members": members}, config, data)

    def test_plans_optout_and_skips(self):
        out, albums, stats = self.build([
            member("1", "Alice", ["r1"]),
            member("2", " Bob ", ["r1", "r3"]),          # 高い方のプラン・前後の空白は除く
            member("3", "ひみつさん", ["f1", "op"]),        # 載せない → ハッシュ
            member("4", None, ["r2"]),                     # ニックネーム無し → 載せない
            member("5", "Carol", ["xx"]),                  # 契約していない
            member("6", "BotName", ["r3"], bot=True),      # Bot は除く
        ])
        self.assertEqual(out, [
            {"name": "Bob", "plan": 3},
            {"name": "Alice", "plan": 1},
            {"hash": br.name_hash("ひみつさん"), "plan": 1},
        ])
        self.assertEqual(albums, [])
        self.assertEqual((stats["no_nick"], stats["panel"], stats["hidden"]), (1, 2, 1))

    def test_hash_rule(self):
        # 前後の空白だけ除いて SHA-256（大文字小文字は区別する）
        expected = hashlib.sha256("GinGira".encode("utf-8")).hexdigest()
        self.assertEqual(br.name_hash("  GinGira "), expected)
        self.assertNotEqual(br.name_hash("gingira"), expected)

    def test_overrides_go_to_roster_without_photo_location(self):
        with tempfile.TemporaryDirectory() as d:
            data = br.load_data(make_data(Path(d), {
                "1": {"photo": "p.png", "slot": 2, "portal": WORLD},
                "2": {"photo": "p.png", "slot": 2},              # 枠の重なり
                "3": {"photo": "nai.png"},                       # 写真が無い
                "4": {"slot": 3},                                # 写真なしの枠
                "9": {"photo": "p.png"},                         # 契約していない人
            }), CONFIG)
            out, albums, stats = self.build([
                member("1", "Alice", ["r3"]), member("2", "Bob", ["r3"]),
                member("3", "Carol", ["r3"]), member("4", "Dave", ["r3"]),
            ], data)
        self.assertEqual(out[0], {"name": "Alice", "plan": 3, "slot": 2, "portal": WORLD})
        self.assertEqual(out[1], {"name": "Bob", "plan": 3})       # 枠は外れるがアルバムは残る
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("p.png", text)                             # 写真のファイル名は名簿に出ない
        self.assertEqual(sorted((a["uid"], a["slot"]) for a in albums), [("1", 2), ("2", None)])
        self.assertEqual(stats["unused_overrides"], 1)
        self.assertEqual(len(stats["warnings"]), 3)
        for w in stats["warnings"]:                                  # 警告に ID を出さない
            self.assertNotIn("ID", w)

    def test_duplicate_names(self):
        out, _, stats = self.build([
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

    def test_secret_checks(self):
        with tempfile.TemporaryDirectory() as d:
            data = make_data(Path(d), {})
            (data / "secret.json").write_text(json.dumps({"link_secret": SECRET, "slot_names": SLOTS[:3]}),
                                              encoding="utf-8")
            with self.assertRaises(br.RosterError):
                br.load_data(data, CONFIG)                  # 枠の名前が足りない

    def test_link_names(self):
        a = br.album_link_name(SECRET, "1", 1)
        self.assertEqual(len(a), 24)
        self.assertEqual(a, br.album_link_name(SECRET, "1", 1))       # 同じ人なら毎回同じ
        self.assertNotEqual(a, br.album_link_name(SECRET, "1", 2))    # version を上げると変わる
        self.assertNotEqual(a, br.album_link_name("x" * 32, "1", 1))  # 秘密の値が違えば推測できない

    def test_change_detection(self):
        members = [{"name": "Alice", "plan": 1}]
        first, changed = br.decide_roster(None, members, "rev1")
        self.assertTrue(changed)
        again, changed = br.decide_roster(first, members, "rev1")
        self.assertFalse(changed)
        self.assertEqual(again["updated"], first["updated"])
        _, changed = br.decide_roster(first, members, "rev2")         # 写真だけ変わっても出し直す
        self.assertTrue(changed)
        _, changed = br.decide_roster(first, members + [{"name": "Bob", "plan": 2}], "rev1")
        self.assertTrue(changed)

    @unittest.skipIf(Image is None, "Pillow が無い")
    def test_main_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            data = make_data(d, {"1": {"photo": "p.png", "slot": 3}, "2": {"photo": "p.png"}})
            # 写真に撮影場所などの付加情報を付けておき、消えるかを確かめる
            im = Image.new("RGB", (1200, 800), (10, 120, 200))
            exif = Image.Exif()
            exif[0x010F] = "SecretCameraMaker"
            im.save(data / "photos" / "p.png", exif=exif.tobytes())
            (d / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
            (d / "guild.json").write_text(json.dumps(
                {"roles": ROLES, "members": [member("1", "Alice", ["r3"]),
                                              member("2", "Hidden", ["r3", "op"])]},
                ensure_ascii=False), encoding="utf-8")
            site = d / "site"
            args = ["--config", str(d / "config.json"), "--data", str(data), "--site", str(site),
                    "--from-file", str(d / "guild.json"), "--previous-file", str(d / "prev.json")]
            out_env = d / "gh_output.txt"
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(out_env)
            try:
                self.assertEqual(br.main(args), 0)                  # 初回 → 変わった
                roster = json.loads((site / "roster.json").read_text(encoding="utf-8"))
                (d / "prev.json").write_text(json.dumps(roster), encoding="utf-8")
                self.assertEqual(br.main(args), 0)                  # 2 回目 → 変わらない
            finally:
                if old is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old
            self.assertEqual(out_env.read_text(encoding="utf-8").split(), ["changed=true", "changed=false"])

            self.assertEqual(roster["members"], [
                {"name": "Alice", "plan": 3, "slot": 3},
                {"hash": br.name_hash("Hidden"), "plan": 3},
            ])
            self.assertEqual(len(roster["rev"]), 16)
            alice = site / "a" / f"{br.album_link_name(SECRET, '1', 1)}.jpg"
            hidden = site / "a" / f"{br.album_link_name(SECRET, '2', 1)}.jpg"
            slot3 = site / "s" / f"{SLOTS[2]}.jpg"
            self.assertTrue(alice.is_file() and hidden.is_file() and slot3.is_file())
            self.assertEqual(alice.read_bytes(), slot3.read_bytes())   # 枠には本人の写真を写す
            self.assertEqual(len(list((site / "s").iterdir())), 1)    # 使っていない枠は出さない
            with Image.open(alice) as im:
                self.assertEqual(max(im.size), 256)                    # 長い辺を縮める
                self.assertEqual(im.format, "JPEG")
                self.assertEqual(len(im.getexif()), 0)                 # 付加情報は消える
            self.assertNotIn(b"SecretCameraMaker", alice.read_bytes())

    def test_real_config_is_placeholder(self):
        # 実物の config.json はロール名がまだ仮なので、はっきりエラーになること
        config = json.loads((br.ROOT / "config.json").read_text(encoding="utf-8"))
        with self.assertRaises(br.RosterError):
            self.build([], config=config)


if __name__ == "__main__":
    unittest.main()
