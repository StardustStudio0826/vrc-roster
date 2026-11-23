#!/usr/bin/env python3
"""VRChat ワールド用の契約者名簿（roster.json）と写真立ての画像を作る。

入力
- Discord: 契約者 = config.json の plans に書いたロールを持つメンバー。VRChat 名 = サーバー内のニックネーム
- 非公開リポジトリ vrc-roster-data（--data）:
  - overrides.json  契約者ごとの手直し（Discord のユーザー ID ごと）: 写真・枠の番号・ポータルの行き先
  - secret.json     リンクの名前を作る秘密の値と、ワールドに入れる枠のファイル名
  - photos/         写真の元のファイル

出力（Git には入れず、GitHub Pages にだけ出す。公開リポジトリの履歴に名前や写真を残さないため）
- roster.json        名簿。パネルに載せない人は名前の代わりにハッシュ。枠の番号は載るが写真の場所は載らない
- a/<推測されにくい名前>.jpg  本人に渡すアルバムのリンク（ワールドの URL 欄に貼る方式）
- s/<推測されにくい名前>.jpg  ワールドに入れておく枠（自動表示の方式）。割り当てた人の写真を写す

公開リポジトリで動くので、ログには人の名前・ID・リンクを出さない（件数だけ）。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0)"
ROSTER_VERSION = 1
DEFAULT_BASE_URL = "https://starduststudio0826.github.io/vrc-roster"
WORLD_ID_RE = re.compile(
    r"^wrld_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
PHOTO_FORMATS = ["PNG", "JPEG", "WEBP"]   # 中身がこの 3 つ以外の画像は開かない（拡張子ではなく中身で判定）
PHOTO_MAX_BYTES = 20 * 1024 * 1024       # 元の写真のファイルの大きさの上限
PHOTO_MAX_PIXELS = 40_000_000            # 元の写真の画素数の上限（展開すると巨大になる画像を弾く）
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"   # 公開の実行記録に出る場かどうか


class RosterError(Exception):
    """名簿を作れないときのエラー（メッセージはそのまま利用者に見せる）。"""


# ---------------------------------------------------------------- 名前・ハッシュ・リンク

def normalize_name(name: str) -> str:
    """照合用の形にそろえる: 前後の空白を除くだけ（大文字小文字は区別する）。

    VRChat の表示名が大文字小文字の違いだけで別人になれるかは未確認なので、
    取り違えが起きない「完全一致」にしておく。ワールド側も同じ手順で比べる。
    """
    return name.strip()


def name_hash(name: str) -> str:
    """normalize_name した名前の UTF-8 を SHA-256 にかけた 16 進（小文字 64 文字）。"""
    return hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()


def album_link_name(secret: str, uid: str, version: int) -> str:
    """本人用アルバムのファイル名（秘密の値から作るので、知らない人には推測できない）。

    version を上げると、同じ人でも別のリンクになる（リンクが広まったときの作り直し用）。
    """
    msg = f"album:{uid}:{version}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:24]


# ---------------------------------------------------------------- Discord から読む

def discord_get(path: str, token: str, what: str):
    url = API + path
    for _ in range(6):
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.load(e).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            if e.code == 401:
                raise RosterError(
                    "Bot のトークンが違います（401）。"
                    "GitHub の Secrets の DISCORD_BOT_TOKEN を確かめてください。"
                ) from None
            if e.code == 403:
                raise RosterError(
                    f"{what}を読む許可がありません（403）。Bot がサーバーに入っているか、"
                    "Developer Portal で Server Members Intent がオンかを確かめてください。"
                ) from None
            if e.code == 404:
                raise RosterError(
                    f"{what}が見つかりません（404）。"
                    "GitHub の Secrets の DISCORD_GUILD_ID（サーバー ID）を確かめてください。"
                ) from None
            raise RosterError(f"{what}の取得で Discord から {e.code} が返りました。") from None
        except urllib.error.URLError as e:
            raise RosterError(f"Discord につながりませんでした: {e.reason}") from None
    raise RosterError(f"{what}の取得が混み合って終わりませんでした。次の回に任せます。")


def fetch_guild(token: str, guild_id: str) -> dict:
    roles = discord_get(f"/guilds/{guild_id}/roles", token, "サーバーのロール")
    members: list = []
    after = "0"
    while True:
        page = discord_get(
            f"/guilds/{guild_id}/members?limit=1000&after={after}",
            token,
            "サーバーのメンバー",
        )
        members.extend(page)
        if len(page) < 1000:
            break
        after = page[-1]["user"]["id"]
    return {"roles": roles, "members": members}


# ---------------------------------------------------------------- 非公開データを読む

def load_data(data_dir: Path | None, config: dict) -> dict:
    """非公開リポジトリの中身を読む。--data が無いとき（試し）は空として扱う。"""
    if data_dir is None:
        return {"overrides": {}, "secret": None, "slot_names": [], "photos_dir": None}
    try:
        overrides = json.loads((data_dir / "overrides.json").read_text(encoding="utf-8"))
        secret = json.loads((data_dir / "secret.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RosterError(f"非公開データに {Path(e.filename).name} がありません。") from None
    link_secret = secret.get("link_secret", "")
    slot_names = secret.get("slot_names", [])
    if len(link_secret) < 32:
        raise RosterError("secret.json の link_secret が短すぎます（32 文字以上）。")
    slots = int(config.get("photo_slots", 0))
    if len(slot_names) < slots:
        raise RosterError(
            f"secret.json の slot_names が {len(slot_names)} 個しかありません"
            f"（config.json の photo_slots は {slots}）。"
        )
    if len(set(slot_names)) != len(slot_names) or any(
        not re.fullmatch(r"[0-9a-z]{16,64}", s) for s in slot_names
    ):
        raise RosterError("secret.json の slot_names は、重ならない英小文字と数字（16 文字以上）にしてください。")
    return {
        "overrides": overrides.get("members", {}),
        "secret": link_secret,
        "slot_names": slot_names,
        "photos_dir": data_dir / "photos",
    }


# ---------------------------------------------------------------- 名簿を組み立てる

def resolve_plans(config: dict, roles: list) -> tuple[dict, str | None]:
    """ロール名 → ID に直し、{ロールID: プラン番号} と「載せない」ロールの ID を返す。"""
    id_by_name: dict[str, list[str]] = {}
    for r in roles:
        id_by_name.setdefault(r["name"], []).append(r["id"])

    missing: list[str] = []
    ambiguous: list[str] = []
    plan_of_role: dict[str, int] = {}
    for entry in config["plans"]:
        plan = int(entry["plan"])
        for role_name in entry["roles"]:
            ids = id_by_name.get(role_name, [])
            if not ids:
                missing.append(role_name)
            elif len(ids) > 1:
                ambiguous.append(role_name)
            else:
                plan_of_role[ids[0]] = max(plan, plan_of_role.get(ids[0], 0))

    optout_name = config.get("optout_role")
    optout_id = None
    if optout_name:
        ids = id_by_name.get(optout_name, [])
        if not ids:
            missing.append(optout_name)
        elif len(ids) > 1:
            ambiguous.append(optout_name)
        else:
            optout_id = ids[0]

    problems = []
    if missing:
        problems.append("サーバーに無いロール名: " + " / ".join(missing))
    if ambiguous:
        problems.append("同じ名前のロールが 2 つ以上ある: " + " / ".join(ambiguous))
    if problems:
        raise RosterError(
            "config.json のロール名がサーバーと合いません。" + "。".join(problems)
        )
    return plan_of_role, optout_id


def check_override(uid: str, ov: dict, config: dict, data: dict, warnings: list[str]) -> dict:
    """手直しの 1 人分を確かめる（おかしい項目は捨てて警告。警告に ID は出さない）。

    返す dict: slot（名簿に載る）・portal（名簿に載る）・photo / version（写真の処理用・名簿には載らない）
    """
    out: dict = {}
    if not isinstance(ov, dict):
        warnings.append("overrides.json に、書き方が崩れている人がいます（{ } で囲んだ形にしてください）")
        return out
    slots = int(config.get("photo_slots", 0))
    if "photo" in ov:
        photo = ov["photo"]
        pdir = data.get("photos_dir")
        if (isinstance(photo, str) and pdir is not None and "/" not in photo and "\\" not in photo
                and Path(photo).suffix.lower() in PHOTO_EXTS and (pdir / photo).is_file()):
            out["photo"] = photo
            version = ov.get("link_version", 1)
            out["version"] = version if isinstance(version, int) and version >= 1 else 1
        else:
            warnings.append("写真のファイルが photos/ に無いか、形式が PNG・JPG・WebP ではない人がいます")
    if "slot" in ov:
        slot = ov["slot"]
        if not (isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= slots):
            warnings.append(f"枠の番号（slot）が 1〜{slots} の整数になっていない人がいます")
        elif "photo" not in out:
            warnings.append("枠（slot）はあるのに写真が無い人がいます（枠は使いません）")
        else:
            out["slot"] = slot
    if "portal" in ov:
        portal = ov["portal"]
        if isinstance(portal, str) and WORLD_ID_RE.match(portal):
            out["portal"] = portal
        else:
            warnings.append("ポータルの行き先（portal）が wrld_ で始まるワールド ID になっていない人がいます")
    return out


def build_members(guild: dict, config: dict, data: dict) -> tuple[list, list, dict]:
    """名簿の行・写真の処理リスト・件数を返す。写真の処理リストは公開しない。"""
    plan_of_role, optout_id = resolve_plans(config, guild["roles"])
    ov_all: dict = data.get("overrides", {})

    stats = {"subscribers": 0, "panel": 0, "hidden": 0, "no_nick": 0,
             "duplicates": 0, "unused_overrides": 0, "albums": 0, "slots_used": 0,
             "bad_name": 0, "bad_photos": 0}
    warnings: list[str] = []

    groups: dict[str, list[dict]] = {}
    seen_uids: set[str] = set()
    for m in guild["members"]:
        user = m.get("user") or {}
        if user.get("bot"):
            continue
        plan = max((plan_of_role.get(r, 0) for r in m.get("roles", [])), default=0)
        if plan == 0:
            continue
        uid = str(user.get("id", ""))
        name = (m.get("nick") or "").strip()
        if not name:
            stats["no_nick"] += 1
            continue
        if any(unicodedata.category(c) in ("Cc", "Cf") for c in name):
            # 改行・見えない文字入りの名前は載せない（パネルの表示崩しや、見た目が同じ別の名前を防ぐ）
            stats["bad_name"] += 1
            continue
        seen_uids.add(uid)
        hidden = optout_id is not None and optout_id in m.get("roles", [])
        extra = check_override(uid, ov_all[uid], config, data, warnings) if uid in ov_all else {}

        groups.setdefault(normalize_name(name), []).append(
            {"name": name, "plan": plan, "hidden": hidden, "uid": uid, "extra": extra})

    stats["unused_overrides"] = sum(1 for uid in ov_all if uid not in seen_uids)

    # 写真（本人用リンク）は Discord の ID ごとに作る。名前が重なっても、他人の写真は消えない。
    albums: list[dict] = []
    by_key: dict[str, dict] = {}
    for key, entries in groups.items():
        for e in entries:
            if "photo" in e["extra"]:
                albums.append({"uid": e["uid"], "photo": e["extra"]["photo"],
                               "version": e["extra"]["version"], "slot": None})
        if len(entries) == 1:
            e = entries[0]
            by_key[key] = {"name": e["name"], "plan": e["plan"], "hidden": e["hidden"],
                           "uid": e["uid"], "extra": dict(e["extra"])}
            continue
        # 同じ名前が 2 人以上（他人の名前をニックネームにした人がいるかもしれない）。
        # ワールドは名前でしか見分けられないので、取り違えが害にならない方に倒す:
        # プランは高い方・「載せない」はどちらかにあれば優先・枠とポータルは、
        # 設定を持つ人が 1 人だけのときだけ使う（2 人以上が持っていたら、どちらも使わない）。
        stats["duplicates"] += len(entries) - 1
        owners = [e for e in entries if "slot" in e["extra"] or "portal" in e["extra"]]
        if len(owners) == 1:
            extra = {k: v for k, v in owners[0]["extra"].items() if k in ("slot", "portal")}
            owner_uid = owners[0]["uid"]
        else:
            extra, owner_uid = {}, ""
        by_key[key] = {"name": entries[0]["name"], "plan": max(e["plan"] for e in entries),
                       "hidden": any(e["hidden"] for e in entries),
                       "uid": owner_uid, "extra": extra}
    if stats["duplicates"]:
        warnings.append("同じ名前の契約者が複数います。他人の名前を使っていないか Discord で確かめてください"
                        "（重なっている間、枠とポータルは設定を持つ人が 1 人のときだけ有効です）")

    # 枠の重なりを確かめる（名前順で先の人を残す）
    used_slots: set[int] = set()
    for key in sorted(by_key):
        slot = by_key[key]["extra"].get("slot")
        if slot is None:
            continue
        if slot in used_slots:
            warnings.append(f"枠 {slot} が 2 人以上に割り当てられています（1 人分だけ残しました）")
            del by_key[key]["extra"]["slot"]
        else:
            used_slots.add(slot)

    slot_of_uid = {e["uid"]: e["extra"]["slot"] for e in by_key.values()
                   if e["uid"] and "slot" in e["extra"]}
    for a in albums:
        a["slot"] = slot_of_uid.get(a["uid"])

    shown, hidden_list = [], []
    for e in by_key.values():
        stats["subscribers"] += 1
        ex = e["extra"]
        public = {k: ex[k] for k in ("slot", "portal") if k in ex}
        if e["hidden"]:
            stats["hidden"] += 1
            hidden_list.append({"hash": name_hash(e["name"]), "plan": e["plan"], **public})
        else:
            stats["panel"] += 1
            shown.append({"name": e["name"], "plan": e["plan"], **public})

    shown.sort(key=lambda x: (-x["plan"], x["name"]))
    hidden_list.sort(key=lambda x: x["hash"])
    stats["albums"] = len(albums)
    stats["slots_used"] = len(used_slots)
    stats["warnings"] = sorted(set(warnings))
    return shown + hidden_list, albums, stats


# ---------------------------------------------------------------- 写真を整えて出す

def make_photo(src: Path, dst: Path, max_px: int) -> None:
    """向きを直し、写真の付加情報（撮影場所など）を消し、長い辺を max_px 以下にして JPG で保存。"""
    from PIL import Image, ImageOps  # 写真を扱うときだけ使う

    if src.stat().st_size > PHOTO_MAX_BYTES:
        raise ValueError("写真のファイルが大きすぎます")
    Image.MAX_IMAGE_PIXELS = PHOTO_MAX_PIXELS
    with Image.open(src, formats=PHOTO_FORMATS) as im:
        if im.width * im.height > PHOTO_MAX_PIXELS:
            raise ValueError("写真の画素数が大きすぎます")
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", quality=88, optimize=True)  # exif を渡さない = 付加情報は残らない


def publish_photos(albums: list, data: dict, config: dict, site: Path, stats: dict) -> str:
    """アルバムと枠の画像を site/ に書き、中身の目印（rev）を返す。

    読めない写真は 1 枚ずつ飛ばす（1 枚のせいで名簿全体が止まらないように）。
    公開の実行記録にはファイル名を出さない（名前が入っていることがあるため）。
    """
    max_px = int(config.get("photo_max_px", 1024))
    digest = hashlib.sha256()
    for a in sorted(albums, key=lambda x: album_link_name(data["secret"], x["uid"], x["version"])):
        link = album_link_name(data["secret"], a["uid"], a["version"])
        album_path = site / "a" / f"{link}.jpg"
        try:
            make_photo(data["photos_dir"] / a["photo"], album_path, max_px)
        except Exception as e:  # 壊れた画像・対応していない形式・大きすぎる画像など
            stats["bad_photos"] += 1
            album_path.unlink(missing_ok=True)
            if not IN_CI:
                print(f"読めない写真: {a['photo']}（{type(e).__name__}: {e}）", file=sys.stderr)
            continue
        body = album_path.read_bytes()
        digest.update(b"a/" + link.encode() + b"\0" + body)
        if a["slot"] is not None:
            slot_name = data["slot_names"][a["slot"] - 1]
            slot_path = site / "s" / f"{slot_name}.jpg"
            slot_path.parent.mkdir(parents=True, exist_ok=True)
            slot_path.write_bytes(body)
            digest.update(b"s/" + slot_name.encode() + b"\0" + body)
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------- 書き出し

def fetch_previous(url: str) -> dict | None:
    """いま公開中の名簿を読む（初回や読めないときは None = 変わったとみなす）。"""
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}t={int(time.time())}",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.load(res)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def decide_roster(previous: dict | None, members: list, rev: str = "") -> tuple[dict, bool]:
    """前の名簿と比べ、書き出す中身と「変わったか」を返す。変わらなければ updated も前のまま。

    rev は写真の中身の目印。名簿が同じでも写真が変われば「変わった」になる。
    """
    if (previous and previous.get("version") == ROSTER_VERSION
            and previous.get("members") == members and previous.get("rev", "") == rev
            and previous.get("updated")):
        return {"version": ROSTER_VERSION, "updated": previous["updated"], "rev": rev,
                "members": members}, False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"version": ROSTER_VERSION, "updated": now, "rev": rev, "members": members}, True


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def report(stats: dict, changed: bool) -> str:
    lines = [
        f"契約者 {stats['subscribers']} 人（パネルに載る {stats['panel']}・載せない {stats['hidden']}）",
        f"写真のある人 {stats['albums']}・使っている枠 {stats['slots_used']}",
        f"ニックネーム未設定で載せなかった人: {stats['no_nick']}",
        f"見えない文字が入った名前で載せなかった人: {stats['bad_name']}",
        f"読めなくて飛ばした写真: {stats['bad_photos']}",
        f"名前が重なった人: {stats['duplicates']}",
        f"契約していない人への手直し（無視）: {stats['unused_overrides']}",
        "名簿: " + ("更新しました" if changed else "変わりなし"),
    ]
    for w in stats["warnings"]:
        lines.append("注意: " + w)
    return "\n".join(lines)


def print_links(data_dir: Path, config: dict, base_url: str) -> None:
    """手元用: 本人に渡すアルバムのリンクと、ワールドに入れる枠の URL を出す（公開の場では使わない）。"""
    data = load_data(data_dir, config)
    base = base_url.rstrip("/")
    print("■ ワールドに入れる枠の URL（写真立ての自動表示用）")
    for i, name in enumerate(data["slot_names"][: int(config.get("photo_slots", 0))], start=1):
        print(f"  枠 {i:2d}: {base}/s/{name}.jpg")
    print("■ 本人に渡すアルバムのリンク（Discord のユーザー ID ごと）")
    for uid, ov in sorted(data["overrides"].items()):
        if "photo" not in ov:
            continue
        version = ov.get("link_version", 1)
        version = version if isinstance(version, int) and version >= 1 else 1
        memo = f"  （{ov['memo']}）" if isinstance(ov.get("memo"), str) else ""
        print(f"  {uid}: {base}/a/{album_link_name(data['secret'], uid, version)}.jpg{memo}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Discord のロールから VRChat 用の名簿と写真立ての画像を作る")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument("--data", help="非公開リポジトリ vrc-roster-data の場所")
    p.add_argument("--site", default=str(ROOT / "site"),
                   help="書き出し先（Git には入れない。Pages にだけ出す）")
    p.add_argument("--previous-url", help="いま公開中の名簿の URL（変わったかの判定に使う）")
    p.add_argument("--previous-file", help="試し用: 前の名簿をファイルから読む")
    p.add_argument("--from-file", help="Discord の代わりに読む試し用のファイル（roles と members）")
    p.add_argument("--print-links", action="store_true",
                   help="手元用: 枠の URL と本人用のリンクを表示して終わる")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = p.parse_args(argv)

    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        data_dir = Path(args.data) if args.data else None
        if args.print_links:
            if data_dir is None:
                raise RosterError("--print-links には --data（非公開リポジトリの場所）が要ります。")
            print_links(data_dir, config, args.base_url)
            return 0
        data = load_data(data_dir, config)
        if args.from_file:
            guild = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        else:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
            if not token or not guild_id:
                raise RosterError(
                    "DISCORD_BOT_TOKEN と DISCORD_GUILD_ID が設定されていません"
                    "（GitHub の Settings → Secrets and variables → Actions）。"
                )
            guild = fetch_guild(token, guild_id)

        members, albums, stats = build_members(guild, config, data)
        site = Path(args.site)
        rev = publish_photos(albums, data, config, site, stats) if albums else ""
        if args.previous_file:
            p_path = Path(args.previous_file)
            previous = json.loads(p_path.read_text(encoding="utf-8")) if p_path.is_file() else None
        elif args.previous_url:
            previous = fetch_previous(args.previous_url)
        else:
            previous = None
        roster, changed = decide_roster(previous, members, rev)
        write_json(site / "roster.json", roster)
        _output("changed", "true" if changed else "false")
        text = report(stats, changed)
    except RosterError as e:
        text = "名簿を作れませんでした: " + str(e)
        print(text, file=sys.stderr)
        _summary(text)
        return 1
    except json.JSONDecodeError as e:
        text = f"JSON の書き方が崩れています（{e.msg}・{e.lineno} 行目）。config.json か overrides.json を確かめてください。"
        print(text, file=sys.stderr)
        _summary(text)
        return 1
    except Exception as e:
        # 想定外のエラー。公開の実行記録には種類だけ出す（詳しい中身に名前やパスが混ざることがあるため）。
        if not IN_CI:
            raise
        text = f"名簿を作れませんでした: 想定外のエラー（{type(e).__name__}）。手元で同じコマンドを動かすと詳しく見られます。"
        print(text, file=sys.stderr)
        _summary(text)
        return 1

    print(text)
    _summary(text)
    return 0


def _output(key: str, value: str) -> None:
    """GitHub Actions の次の手順へ値を渡す。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def _summary(text: str) -> None:
    """GitHub Actions の実行結果の画面にも出す（件数だけ・名前は出さない）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")


if __name__ == "__main__":
    sys.exit(main())
