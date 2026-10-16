"""
Mercari Japan 新品追踪器 —— 关键词上新时推送到 Telegram。

用法:
    python mercari_watch.py              # 持续运行，按 config.json 的间隔轮询
    python mercari_watch.py --once       # 只检查一次（适合配合计划任务）
    python mercari_watch.py --test       # 发一条测试消息到 Telegram 并打印搜索结果
"""
import argparse
import base64
import json
import logging
import os
import socket
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "seen.json"
LOG_PATH = BASE_DIR / "watch.log"
LOCK_PORT = 47231

SEARCH_URL = "https://api.mercari.jp/v2/entities:search"

log = logging.getLogger("mercari_watch")


# ---------------------------------------------------------------- Mercari API

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class MercariClient:
    """Mercari 网页版使用的搜索接口，需要 DPoP (ES256 JWT) 请求头。"""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        nums = self.key.public_key().public_numbers()
        self.jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64url(nums.x.to_bytes(32, "big")),
            "y": _b64url(nums.y.to_bytes(32, "big")),
        }
        self.device_uuid = str(uuid.uuid4())
        self.session = requests.Session()

    def _dpop(self, url: str, method: str) -> str:
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}
        payload = {
            "iat": int(time.time()),
            "jti": str(uuid.uuid4()),
            "htu": url,
            "htm": method,
            "uuid": self.device_uuid,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode())
        )
        der = self.key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return signing_input + "." + _b64url(sig)

    def search(self, keyword, exclude="", price_min=0, price_max=0, page_size=60):
        body = {
            "userId": "",
            "pageSize": page_size,
            "pageToken": "",
            "searchSessionId": uuid.uuid4().hex,
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "thumbnailTypes": [],
            "searchCondition": {
                "keyword": keyword,
                "excludeKeyword": exclude,
                "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_ON_SALE"],
                "sizeId": [],
                "categoryId": [],
                "brandId": [],
                "sellerId": [],
                "priceMin": int(price_min or 0),
                "priceMax": int(price_max or 0),
                "itemConditionId": [],
                "shippingPayerId": [],
                "shippingFromArea": [],
                "shippingMethod": [],
                "colorId": [],
                "hasCoupon": False,
                "attributes": [],
                "itemTypes": [],
                "skuIds": [],
            },
            "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
            "serviceFrom": "suruga",
            "withItemBrand": True,
            "withItemSize": False,
            "withItemPromotions": True,
            "withItemSizes": True,
            "withShopname": False,
        }
        headers = {
            "DPoP": self._dpop(SEARCH_URL, "POST"),
            "X-Platform": "web",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
            ),
        }
        resp = self.session.post(SEARCH_URL, json=body, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json().get("items", [])


def item_url(item) -> str:
    if item.get("itemType") == "ITEM_TYPE_BEYOND":  # Mercari Shops 商品
        return f"https://jp.mercari.com/shops/product/{item['id']}"
    return f"https://jp.mercari.com/item/{item['id']}"


def item_photo(item):
    # 普通商品用 jpg 原图（Telegram 对 webp 支持不稳定），Shops 商品用接口给的缩略图
    if item.get("itemType") != "ITEM_TYPE_BEYOND":
        return f"https://static.mercdn.net/item/detail/orig/photos/{item['id']}_1.jpg"
    return (item.get("thumbnails") or [None])[0]


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, chat_id):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send(self, text, photo=None):
        try:
            if photo:
                r = requests.post(
                    f"{self.api}/sendPhoto",
                    data={"chat_id": self.chat_id, "photo": photo,
                          "caption": text, "parse_mode": "HTML"},
                    timeout=20,
                )
                if r.ok:
                    return True
                log.warning("sendPhoto 失败，改发文本: %s", r.text[:200])
            r = requests.post(
                f"{self.api}/sendMessage",
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=20,
            )
            if not r.ok:
                log.error("Telegram 发送失败: %s", r.text[:300])
            return r.ok
        except requests.RequestException as e:
            log.error("Telegram 网络错误: %s", e)
            return False


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_item(item, keyword, jpy_rate=None) -> str:
    price = int(item.get("price", 0))
    price_str = f"¥{price:,}"
    if jpy_rate:
        price_str += f"  (≈{price * jpy_rate:,.0f})"
    return (
        f"🆕 <b>[{html_escape(keyword)}]</b>\n"
        f"{html_escape(item.get('name', ''))}\n"
        f"💴 {price_str}\n"
        f"{item_url(item)}"
    )


# ---------------------------------------------------------------- 状态 / 主循环

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def check_once(client, tg, config, state):
    """返回搜索失败的关键词数量。"""
    failures = 0
    for w in config["watches"]:
        kw = w["keyword"]
        key = json.dumps([kw, w.get("exclude", ""), w.get("price_min", 0),
                          w.get("price_max", 0)], ensure_ascii=False)
        try:
            items = client.search(kw, w.get("exclude", ""),
                                  w.get("price_min", 0), w.get("price_max", 0))
        except Exception as e:
            log.error("搜索 '%s' 失败: %s", kw, e)
            failures += 1
            continue

        seen = set(state.get(key, []))
        first_run = key not in state
        new_items = [it for it in items if it["id"] not in seen]

        if first_run:
            # 首次运行只记录现有商品，不推送，避免一次刷屏
            log.info("[%s] 首次运行，记录 %d 个现有商品", kw, len(items))
        else:
            for it in reversed(new_items):  # 旧 → 新 顺序推送
                thumb = item_photo(it)
                tg.send(format_item(it, kw, config.get("price_rate")), thumb)
                log.info("[%s] 新商品: %s ¥%s", kw, it.get("name"), it.get("price"))
                time.sleep(0.5)
            if not new_items:
                log.info("[%s] 无新商品", kw)

        # 只保留最近一批的 id，防止文件无限增长
        merged = [it["id"] for it in items] + [i for i in state.get(key, [])
                                                 if i not in {x["id"] for x in items}]
        state[key] = merged[:500]
        save_state(state)
        time.sleep(config.get("delay_between_keywords", 3))
    return failures


def main():
    parser = argparse.ArgumentParser(description="Mercari 关键词上新 Telegram 通知")
    parser.add_argument("--once", action="store_true", help="只检查一次后退出")
    parser.add_argument("--test", action="store_true", help="发送测试消息并打印搜索结果")
    args = parser.parse_args()

    # 同时写到 watch.log（pythonw 后台运行时没有控制台，只能看日志文件）
    handlers = [RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1,
                                    encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m-%d %H:%M:%S", handlers=handlers)

    config = load_json(CONFIG_PATH, None)
    if not config:
        sys.exit(f"找不到或无法解析 {CONFIG_PATH}，请先复制 config.example.json 为 config.json 并填写")

    # token / chat_id 只从环境变量读取（GitHub Actions 用 Secrets 注入），绝不写进仓库文件
    if "telegram_bot_token" in config or "telegram_chat_id" in config:
        sys.exit("config.json 里不能写 token / chat_id（仓库是公开的），请删掉并改用 Secrets")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("缺少 Telegram 配置：请设置环境变量 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    tg = Telegram(token, chat_id)
    client = MercariClient()

    if args.test:
        w = config["watches"][0]
        items = client.search(w["keyword"], w.get("exclude", ""),
                              w.get("price_min", 0), w.get("price_max", 0))
        print(f"'{w['keyword']}' 搜到 {len(items)} 个在售商品，最新 5 个：")
        for it in items[:5]:
            print(f"  ¥{int(it['price']):>8,}  {it['name']}  {item_url(it)}")
        if items:
            it = items[0]
            ok = tg.send("✅ Mercari 追踪器测试消息\n\n" + format_item(it, w["keyword"],
                         config.get("price_rate")), item_photo(it))
            print("已发送测试消息到 Telegram" if ok else "❌ Telegram 发送失败，请检查上面的错误信息")
            if not ok:
                sys.exit(1)
        return

    state = load_json(STATE_PATH, {})
    interval = config.get("interval_seconds", 120)

    if args.once:
        # 全部关键词都搜索失败时以非零状态退出，让 GitHub Actions 标红并发邮件
        if check_once(client, tg, config, state) == len(config["watches"]):
            sys.exit("所有关键词搜索都失败了，可能是 Mercari 接口变化或请求被拦截")
        return

    # 防止重复启动：占用一个本地端口，第二个实例会绑定失败并退出
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        log.warning("已有一个监控在运行，本次启动退出")
        return

    log.info("开始监控 %d 个关键词，每 %d 秒检查一次", len(config["watches"]), interval)
    while True:
        try:
            # 每轮重新读取配置，修改关键词后无需重启
            config = load_json(CONFIG_PATH, config)
            check_once(client, tg, config, state)
        except Exception:
            log.exception("本轮检查出错，稍后重试")
        time.sleep(config.get("interval_seconds", interval))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
