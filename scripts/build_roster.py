#!/usr/bin/env python3
"""VRChat ワールド用の契約者名簿（roster.json）を Discord のロールから作る。

名簿は Git には入れず、GitHub Pages にだけ出す（公開リポジトリの履歴に名前を残さないため）。

- 契約者 = config.json の plans に書いたロールのどれかを持つメンバー
- VRChat 名 = そのサーバーでのニックネーム（未設定の人は載せない）
- 「パネルに載せない」ロールを持つ人は、名前の代わりにハッシュで載せる
- プラン 3 の写真の枠・ポータルの行き先は overrides.json（Discord のユーザー ID ごと）から足す

公開リポジトリで動くので、ログには人の名前を出さない（件数だけ）。
外部のライブラリは使わない（Python 標準だけ）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com, 1.0)"
ROSTER_VERSION = 1
WORLD_ID_RE = re.compile(
    r"^wrld_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class RosterError(Exception):
    """名簿を作れないときのエラー（メッセージはそのまま利用者に見せる）。"""


# ---------------------------------------------------------------- 名前とハッシュ

def normalize_name(name: str) -> str:
    """照合用の形にそろえる: 前後の空白を除くだけ（大文字小文字は区別する）。

    VRChat の表示名が大文字小文字の違いだけで別人になれるかは未確認なので、
    取り違えが起きない「完全一致」にしておく。ワールド側も同じ手順で比べる。
    """
    return name.strip()


def name_hash(name: str) -> str:
    """normalize_name した名前の UTF-8 を SHA-256 にかけた 16 進（小文字 64 文字）。"""
    return hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()


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


def check_override(uid: str, ov: dict, config: dict, warnings: list[str]) -> dict:
    """手直しの 1 人分を確かめ、名簿に足す項目だけ返す（おかしい項目は捨てて警告）。"""
    extra: dict = {}
    slots = int(config.get("photo_slots", 0))
    if "photo_slot" in ov:
        slot = ov["photo_slot"]
        if isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= slots:
            extra["photo"] = slot
        else:
            warnings.append(f"ID {uid}: photo_slot は 1〜{slots} の整数にしてください")
    if "portal" in ov:
        portal = ov["portal"]
        if isinstance(portal, str) and WORLD_ID_RE.match(portal):
            extra["portal"] = portal
        else:
            warnings.append(f"ID {uid}: portal は wrld_ で始まるワールド ID にしてください")
    return extra


def build_members(guild: dict, config: dict, overrides: dict) -> tuple[list, dict]:
    plan_of_role, optout_id = resolve_plans(config, guild["roles"])
    ov_all: dict = overrides.get("members", {})

    stats = {"subscribers": 0, "panel": 0, "hidden": 0, "no_nick": 0,
             "duplicates": 0, "unused_overrides": 0}
    warnings: list[str] = []

    by_key: dict[str, dict] = {}
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
        seen_uids.add(uid)
        hidden = optout_id is not None and optout_id in m.get("roles", [])
        extra = check_override(uid, ov_all[uid], config, warnings) if uid in ov_all else {}

        key = normalize_name(name)
        if key in by_key:
            # 同じ名前が 2 人以上。プランは高い方、載せない指定はどちらかにあれば優先。
            stats["duplicates"] += 1
            prev = by_key[key]
            prev["plan"] = max(prev["plan"], plan)
            prev["hidden"] = prev["hidden"] or hidden
            for k, v in extra.items():
                prev["extra"].setdefault(k, v)
            continue
        by_key[key] = {"name": name, "plan": plan, "hidden": hidden, "extra": extra}

    stats["unused_overrides"] = sum(1 for uid in ov_all if uid not in seen_uids)

    # 写真の枠の重なりを確かめる（先に来た人を残す）
    used_slots: dict[int, str] = {}
    for key in sorted(by_key):
        slot = by_key[key]["extra"].get("photo")
        if slot is None:
            continue
        if slot in used_slots:
            warnings.append(f"写真の枠 {slot} が 2 人以上に割り当てられています（1 人分だけ残しました）")
            del by_key[key]["extra"]["photo"]
        else:
            used_slots[slot] = key

    shown, hidden_list = [], []
    for e in by_key.values():
        stats["subscribers"] += 1
        if e["hidden"]:
            stats["hidden"] += 1
            hidden_list.append({"hash": name_hash(e["name"]), "plan": e["plan"], **e["extra"]})
        else:
            stats["panel"] += 1
            shown.append({"name": e["name"], "plan": e["plan"], **e["extra"]})

    shown.sort(key=lambda x: (-x["plan"], normalize_name(x["name"]), x["name"]))
    hidden_list.sort(key=lambda x: x["hash"])
    stats["warnings"] = warnings
    return shown + hidden_list, stats


def missing_photo_files(members: list, photos_dir: Path) -> list[int]:
    return sorted(
        m["photo"] for m in members
        if "photo" in m and not (photos_dir / f"{m['photo']:02d}.png").is_file()
    )


# ---------------------------------------------------------------- 書き出し

def fetch_previous(url: str) -> dict | None:
    """いま公開中の名簿を読む（初回や読めないときは None = 変わったとみなす）。

    名簿は Git の記録に残さない（公開リポジトリの履歴から、後で「載せない」にした人の
    名前が読めてしまうため）。変わったかどうかは公開中の物と比べて決める。
    """
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}t={int(time.time())}",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.load(res)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def decide_roster(previous: dict | None, members: list) -> tuple[dict, bool]:
    """前の名簿と比べ、書き出す中身と「変わったか」を返す。変わらなければ updated も前のまま。"""
    if (previous and previous.get("version") == ROSTER_VERSION
            and previous.get("members") == members and previous.get("updated")):
        return {"version": ROSTER_VERSION, "updated": previous["updated"], "members": members}, False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"version": ROSTER_VERSION, "updated": now, "members": members}, True


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def report(stats: dict, changed: bool, missing_photos: list[int]) -> str:
    lines = [
        f"契約者 {stats['subscribers']} 人（パネルに載る {stats['panel']}・載せない {stats['hidden']}）",
        f"ニックネーム未設定で載せなかった人: {stats['no_nick']}",
        f"名前が重なった人: {stats['duplicates']}",
        f"契約していない人への手直し（無視）: {stats['unused_overrides']}",
        "名簿: " + ("更新しました" if changed else "変わりなし"),
    ]
    if missing_photos:
        lines.append("写真のファイルが無い枠: " + ", ".join(f"{s:02d}.png" for s in missing_photos))
    for w in stats["warnings"]:
        lines.append("注意: " + w)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Discord のロールから VRChat 用の名簿を作る")
    p.add_argument("--config", default=str(ROOT / "config.json"))
    p.add_argument("--overrides", default=str(ROOT / "overrides.json"))
    p.add_argument("--out", default=str(ROOT / "site" / "roster.json"),
                   help="書き出し先（Git には入れない。Pages にだけ出す）")
    p.add_argument("--photos", default=str(ROOT / "docs" / "photos"))
    p.add_argument("--previous-url", help="いま公開中の名簿の URL（変わったかの判定に使う）")
    p.add_argument("--previous-file", help="試し用: 前の名簿をファイルから読む")
    p.add_argument("--from-file", help="Discord の代わりに読む試し用のファイル（roles と members）")
    args = p.parse_args(argv)

    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        overrides = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
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

        members, stats = build_members(guild, config, overrides)
        if args.previous_file:
            p_path = Path(args.previous_file)
            previous = json.loads(p_path.read_text(encoding="utf-8")) if p_path.is_file() else None
        elif args.previous_url:
            previous = fetch_previous(args.previous_url)
        else:
            previous = None
        data, changed = decide_roster(previous, members)
        write_json(Path(args.out), data)
        _output("changed", "true" if changed else "false")
        text = report(stats, changed, missing_photo_files(members, Path(args.photos)))
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
