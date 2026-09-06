#!/usr/bin/env python3
"""Solana meme-token signal bot.

Safety model: deterministic risk gates run before the optional AI explanation.
This project never signs transactions, stores wallets, or executes trades.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
STATE_FILE = ROOT / "state.json"
PAPER_STATE_FILE = ROOT / "paper_state.json"
PAPER_LEDGER_FILE = ROOT / "paper_ledger.jsonl"
DEX_BASE = "https://api.dexscreener.com"
RUG_BASE = "https://api.rugcheck.xyz"
TELEGRAM_BASE = "https://api.telegram.org"
SOLANA_NATIVE = "So11111111111111111111111111111111111111112"

LOG = logging.getLogger("meme-signal-bot")
SCAN_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def get_json(url: str, *, timeout: int = 25) -> Any:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "solana-meme-signal-bot/1.0"})
    response.raise_for_status()
    return response.json()


def post_json(url: str, payload: dict[str, Any], *, timeout: int = 25, headers: dict[str, str] | None = None) -> Any:
    response = requests.post(url, json=payload, timeout=timeout, headers=headers or {})
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(data.get("description", "remote API rejected request"))
    if isinstance(data, dict) and data.get("error"):
        error = data.get("error")
        raise RuntimeError(str(error.get("message", error)) if isinstance(error, dict) else str(error))
    return data


def finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def nested_number(obj: dict[str, Any] | None, key: str, default: float = 0.0) -> float:
    return finite_number((obj or {}).get(key), default)


def now_ms() -> int:
    return int(time.time() * 1000)


def age_hours(pair_created_at: Any) -> float | None:
    created = finite_number(pair_created_at, 0)
    if created <= 0:
        return None
    return max(0.0, (now_ms() - created) / 3_600_000)


def fetch_profiles() -> list[dict[str, Any]]:
    """Discover profiles, then keep only Solana tokens and deduplicate mints."""
    candidates: list[dict[str, Any]] = []
    for endpoint in ("/token-profiles/latest/v1", "/token-profiles/recent-updates/v1"):
        try:
            data = get_json(DEX_BASE + endpoint)
            if isinstance(data, list):
                candidates.extend(data)
        except Exception as exc:
            LOG.warning("profile endpoint failed: %s", exc)
    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if item.get("chainId") == "solana" and item.get("tokenAddress"):
            unique[str(item["tokenAddress"])] = item
    return list(unique.values())


def fetch_pairs(addresses: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Fetch up to 30 token addresses per request, as documented by DEX Screener."""
    result: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(addresses), 30):
        batch = addresses[start : start + 30]
        if not batch:
            continue
        try:
            data = get_json(f"{DEX_BASE}/tokens/v1/solana/{','.join(batch)}")
            if isinstance(data, list):
                for pair in data:
                    address = ((pair.get("baseToken") or {}).get("address") or "")
                    if address:
                        result.setdefault(address, []).append(pair)
                    quote = ((pair.get("quoteToken") or {}).get("address") or "")
                    if quote and quote != SOLANA_NATIVE:
                        result.setdefault(quote, []).append(pair)
        except Exception as exc:
            LOG.warning("pair endpoint failed for batch: %s", exc)
    return result


def choose_pair(pairs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    pairs = list(pairs)
    if not pairs:
        return None
    return max(pairs, key=lambda p: nested_number(p.get("liquidity"), "usd"))


def rug_report(mint: str) -> dict[str, Any]:
    return get_json(f"{RUG_BASE}/v1/tokens/{mint}/report")


def known_pool_owners(report: dict[str, Any]) -> set[str]:
    owners: set[str] = set()
    for market in report.get("markets") or []:
        if market.get("pubkey"):
            owners.add(str(market["pubkey"]))
        for field in ("liquidityA", "liquidityB"):
            if market.get(field):
                owners.add(str(market[field]))
    for address, info in (report.get("knownAccounts") or {}).items():
        label = str((info or {}).get("type", "")) + " " + str((info or {}).get("name", ""))
        if any(word in label.upper() for word in ("AMM", "POOL", "LOCKER")):
            owners.add(str(address))
    return owners


def holder_stats(report: dict[str, Any]) -> tuple[float, float, int]:
    pool_owners = known_pool_owners(report)
    percentages = [
        finite_number(holder.get("pct"))
        for holder in (report.get("topHolders") or [])
        if str(holder.get("owner", holder.get("address", ""))) not in pool_owners
    ]
    percentages = [x for x in percentages if x >= 0]
    top1 = max(percentages, default=0.0)
    top5 = sum(sorted(percentages, reverse=True)[:5])
    return top1, top5, len(report.get("topHolders") or [])


def risk_levels(report: dict[str, Any]) -> list[str]:
    return [str(r.get("level", "")).lower() for r in (report.get("risks") or [])]


def social_count(pair: dict[str, Any]) -> int:
    info = pair.get("info") or {}
    return len(info.get("socials") or []) + len(info.get("websites") or [])


def hard_gate(pair: dict[str, Any], report: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    age = age_hours(pair.get("pairCreatedAt"))
    liquidity = nested_number(pair.get("liquidity"), "usd")
    h1_volume = nested_number(pair.get("volume"), "h1")
    if age is None or age > cfg["max_age_hours"]:
        reasons.append("العمر خارج نافذة التوكنات الجديدة")
    if liquidity < cfg["min_liquidity"]:
        reasons.append("السيولة أقل من الحد الأدنى")
    if h1_volume < cfg["min_h1_volume"]:
        reasons.append("حجم الساعة أقل من الحد الأدنى")
    if report.get("rugged") is True:
        reasons.append("RugCheck وضع علامة rugged")
    if any(level in {"danger", "critical", "high"} for level in risk_levels(report)):
        reasons.append("RugCheck يتضمن خطراً عالياً أو حرجاً")
    token = report.get("token") or {}
    if token.get("mintAuthority"):
        reasons.append("mint authority ما زالت مفعّلة")
    if token.get("freezeAuthority"):
        reasons.append("freeze authority ما زالت مفعّلة")
    top1, top5, _ = holder_stats(report)
    if top1 > cfg["max_top_holder_pct"]:
        reasons.append("تركيز مالك كبير بعد استبعاد مجمعات السيولة")
    return not reasons, reasons


def deterministic_score(pair: dict[str, Any], report: dict[str, Any], cfg: dict[str, Any]) -> tuple[float, dict[str, float]]:
    age = age_hours(pair.get("pairCreatedAt")) or cfg["max_age_hours"]
    liquidity = nested_number(pair.get("liquidity"), "usd")
    h1_volume = nested_number(pair.get("volume"), "h1")
    h1_change = nested_number(pair.get("priceChange"), "h1")
    txns = pair.get("txns") or {}
    h1_txns = txns.get("h1") or {}
    buys = nested_number(h1_txns, "buys")
    sells = nested_number(h1_txns, "sells")
    total = buys + sells
    buy_ratio = buys / total if total else 0.0
    top1, top5, _ = holder_stats(report)
    risks = risk_levels(report)
    risk_penalty = min(15.0, 4.0 * sum(level in {"warn", "danger", "critical", "high"} for level in risks))
    components = {
        "freshness": max(0.0, 15.0 * (1.0 - age / cfg["max_age_hours"])),
        "liquidity": min(20.0, 20.0 * math.log10(max(liquidity, 1.0) / cfg["min_liquidity"] + 1.0)),
        "volume": min(15.0, 15.0 * math.log10(max(h1_volume, 1.0) / cfg["min_h1_volume"] + 1.0)),
        "flow": min(10.0, max(0.0, 20.0 * (buy_ratio - 0.5))),
        "momentum": min(15.0, max(0.0, h1_change / 10.0)),
        "liquidity_lock": min(10.0, max(0.0, finite_number(report.get("lpLockedPct")) / 10.0)),
        "socials": min(5.0, float(social_count(pair))),
        "distribution": max(0.0, 10.0 - max(0.0, top1 - 15.0) / 3.0 - max(0.0, top5 - 45.0) / 6.0),
        "risk_penalty": -risk_penalty,
    }
    return max(0.0, min(100.0, sum(components.values()))), components


def normalize_candidate(profile: dict[str, Any], pair: dict[str, Any], report: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    score, components = deterministic_score(pair, report, cfg)
    top1, top5, holder_count = holder_stats(report)
    txns = pair.get("txns") or {}
    h1_txns = txns.get("h1") or {}
    candidate = {
        "mint": profile.get("tokenAddress"),
        "name": (pair.get("baseToken") or {}).get("name") or (pair.get("baseToken") or {}).get("symbol") or "Unknown",
        "symbol": (pair.get("baseToken") or {}).get("symbol") or "?",
        "pair_url": pair.get("url"),
        "dex": pair.get("dexId"),
        "price_usd": finite_number(pair.get("priceUsd")),
        "liquidity_usd": nested_number(pair.get("liquidity"), "usd"),
        "volume_h1_usd": nested_number(pair.get("volume"), "h1"),
        "volume_h24_usd": nested_number(pair.get("volume"), "h24"),
        "change_m5_pct": nested_number(pair.get("priceChange"), "m5"),
        "change_h1_pct": nested_number(pair.get("priceChange"), "h1"),
        "change_h24_pct": nested_number(pair.get("priceChange"), "h24"),
        "buys_h1": int(nested_number(h1_txns, "buys")),
        "sells_h1": int(nested_number(h1_txns, "sells")),
        "fdv_usd": finite_number(pair.get("fdv")),
        "market_cap_usd": finite_number(pair.get("marketCap")),
        "age_hours": age_hours(pair.get("pairCreatedAt")),
        "top_holder_pct_ex_pool": top1,
        "top5_holders_pct_ex_pool": top5,
        "reported_holder_count": report.get("totalHolders"),
        "lp_locked_pct": finite_number(report.get("lpLockedPct"), max((finite_number(((m.get("lp") or {}).get("lpLockedPct"))) for m in (report.get("markets") or [])), default=0.0)),
        "rugcheck_score_normalised": finite_number(report.get("score_normalised")),
        "rugcheck_risks": report.get("risks") or [],
        "risk_levels": risk_levels(report),
        "mint_authority": (report.get("token") or {}).get("mintAuthority"),
        "freeze_authority": (report.get("token") or {}).get("freezeAuthority"),
        "description": profile.get("description") or "",
        "socials": (pair.get("info") or {}).get("socials") or [],
        "websites": (pair.get("info") or {}).get("websites") or [],
        "raw_score": round(score, 2),
        "score_components": {k: round(v, 2) for k, v in components.items()},
    }
    allowed, reasons = hard_gate(pair, report, cfg)
    candidate["hard_gate_passed"] = allowed
    candidate["gate_reasons"] = reasons
    return candidate


def ai_assess(candidate: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Use structured JSON; fall back to deterministic wording if AI is disabled/unavailable."""
    fallback = {
        "action": "WATCH" if candidate["raw_score"] >= cfg["signal_threshold"] - 10 else "AVOID",
        "confidence": int(max(0, min(100, candidate["raw_score"]))),
        "thesis": "المرشح اجتاز بوابة المخاطر الأساسية، لكن النتيجة لا تعني سلامة التوكن أو ضمان الربح.",
        "risks": ["تقلب شديد", "إمكان التلاعب بالحجم والسيولة", *candidate.get("gate_reasons", [])],
        "invalidation": "إلغاء الإشارة إذا هبطت السيولة تحت الحد أو ظهرت مخاطرة حرجة أو انخفض السعر تحت قاع آخر ساعة.",
        "entry_plan": "للمراقبة فقط؛ لا تدخل إذا لم تستطع تحمل خسارة كامل المبلغ.",
        "exit_plan": "أخذ أرباح جزئي تدريجي وإيقاف الخسارة وفق خطة المستخدم، دون تنفيذ آلي.",
        "rationale": "تقييم احتياطي بالقواعد بسبب عدم توفر نموذج الذكاء الاصطناعي.",
    }
    if not cfg["ai_enabled"] or not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_API_BASE"):
        return fallback
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["SIGNAL", "WATCH", "AVOID"]},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "thesis": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "invalidation": {"type": "string"},
            "entry_plan": {"type": "string"},
            "exit_plan": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["action", "confidence", "thesis", "risks", "invalidation", "entry_plan", "exit_plan", "rationale"],
        "additionalProperties": False,
    }
    system = (
        "أنت محلل مخاطر لتوكنات سولانا الجديدة. لا تتنبأ بيقين ولا تعد بعائد. "
        "استخدم البيانات فقط، واعتبر بوابة hard_gate شرطاً ضرورياً. إذا كانت false فاختر AVOID. "
        "إذا كانت true اختر SIGNAL فقط عندما تكون الأدلة متماسكة والدرجة الخام مناسبة، وإلا WATCH. "
        "اكتب بالعربية، واذكر المخاطر بوضوح. لا تقترح تنفيذ صفقة أو رافعة مالية. أخرج JSON فقط."
    )
    prompt = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    payload = {
        "model": os.getenv("AI_MODEL", "gpt-5-mini"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "signal_assessment", "strict": True, "schema": schema}},
        "max_completion_tokens": 900,
    }
    try:
        result = post_json(
            os.getenv("OPENAI_API_BASE").rstrip("/") + "/chat/completions",
            payload,
            timeout=45,
            headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}", "Content-Type": "application/json"},
        )
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if parsed.get("action") == "SIGNAL" and not candidate["hard_gate_passed"]:
            parsed["action"] = "AVOID"
        return parsed
    except Exception as exc:
        LOG.warning("AI assessment failed; using fallback: %s", exc)
        fallback["rationale"] = "تعذر استدعاء النموذج؛ تم استخدام التقييم الاحتياطي بالقواعد."
        return fallback


def signal_levels(candidate: dict[str, Any]) -> dict[str, float | None]:
    price = candidate.get("price_usd") or 0.0
    if price <= 0:
        return {"entry": None, "stop": None, "tp1": None, "tp2": None}
    return {"entry": price, "stop": price * 0.82, "tp1": price * 1.25, "tp2": price * 1.60}


def fmt_usd(value: Any) -> str:
    value = finite_number(value)
    if value == 0:
        return "$0"
    if abs(value) < 0.000001:
        return f"${value:.10g}"
    if abs(value) < 0.01:
        return f"${value:.8f}"
    if abs(value) < 1000:
        return f"${value:,.6f}".rstrip("0").rstrip(".")
    return f"${value:,.0f}"


def format_signal(candidate: dict[str, Any], assessment: dict[str, Any]) -> str:
    levels = signal_levels(candidate)
    action = assessment.get("action", "WATCH")
    title = "إشارة مرشحة" if action == "SIGNAL" else ("مراقبة" if action == "WATCH" else "تجنب")
    risks = assessment.get("risks") or []
    risk_text = "؛ ".join(str(x) for x in risks[:5]) or "لا توجد ملاحظات إضافية من النموذج"
    return (
        f"{title} — {candidate['name']} (${candidate['symbol']})\n"
        f"العنوان: {candidate['mint']}\n"
        f"الإجراء: {action} | الثقة: {assessment.get('confidence', 0)}/100 | الدرجة: {candidate['raw_score']}/100\n"
        f"السعر: {fmt_usd(candidate['price_usd'])} | العمر: {candidate.get('age_hours') or 0:.1f} ساعة\n"
        f"السيولة: {fmt_usd(candidate['liquidity_usd'])} | حجم 1س: {fmt_usd(candidate['volume_h1_usd'])}\n"
        f"التغير: 5د {candidate['change_m5_pct']:.2f}% | 1س {candidate['change_h1_pct']:.2f}% | 24س {candidate['change_h24_pct']:.2f}%\n"
        f"التدفقات 1س: شراء {candidate['buys_h1']} / بيع {candidate['sells_h1']}\n"
        f"المالكون: أعلى مالك خارج المجمع {candidate['top_holder_pct_ex_pool']:.2f}% | أعلى 5 {candidate['top5_holders_pct_ex_pool']:.2f}%\n"
        f"LP مقفلة: {candidate['lp_locked_pct']:.2f}% | مخاطر RugCheck: {', '.join(candidate['risk_levels']) or 'لا توجد مستويات'}\n\n"
        f"الخلاصة: {assessment.get('thesis', '')}\n"
        f"المخاطر: {risk_text}\n"
        f"الإبطال: {assessment.get('invalidation', '')}\n"
        f"خطة الدخول التعليمية: {assessment.get('entry_plan', '')}\n"
        f"خطة الخروج التعليمية: {assessment.get('exit_plan', '')}\n"
        f"مستويات مرجعية غير تنفيذية: دخول {fmt_usd(levels['entry'])} | وقف نظري {fmt_usd(levels['stop'])} | TP1 {fmt_usd(levels['tp1'])} | TP2 {fmt_usd(levels['tp2'])}\n"
        f"الرابط: {candidate.get('pair_url') or ''}\n\n"
        "تنبيه: هذه إشارة بحثية تجريبية وليست نصيحة مالية أو ضماناً، وقد تخسر كامل رأس المال. لا ينفذ هذا البوت صفقات."
    )


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"sent_mints": {}, "last_scan": None, "enabled": True, "alerts_enabled": True, "settings": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"sent_mints": {}, "last_scan": None, "enabled": True, "alerts_enabled": True, "settings": {}}
    except Exception:
        return {"sent_mints": {}, "last_scan": None, "enabled": True, "alerts_enabled": True, "settings": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def paper_default_state() -> dict[str, Any]:
    initial = env_float("PAPER_INITIAL_BALANCE", 200.0)
    return {
        "mode": "paper",
        "enabled": True,
        "initial_balance": initial,
        "cash": initial,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "trades": [],
        "positions": {},
        "wins": 0,
        "losses": 0,
        "settings": {},
        "last_update": None,
    }


def load_paper_state() -> dict[str, Any]:
    default = paper_default_state()
    if not PAPER_STATE_FILE.exists():
        return default
    try:
        loaded = json.loads(PAPER_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("mode") != "paper":
            return default
        for key, value in default.items():
            loaded.setdefault(key, value)
        return loaded
    except Exception:
        return default


def save_paper_state(state: dict[str, Any]) -> None:
    PAPER_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def paper_cfg() -> dict[str, Any]:
    state = load_paper_state()
    settings = state.get("settings") or {}
    cfg = {
        "risk_per_trade": env_float("PAPER_RISK_PER_TRADE", 0.03),
        "max_position_pct": env_float("PAPER_MAX_POSITION_PCT", 1.0),
        "max_positions": env_int("PAPER_MAX_POSITIONS", 2),
        "fee_bps": env_float("PAPER_FEE_BPS", 30),
        "entry_slippage_bps": env_float("PAPER_ENTRY_SLIPPAGE_BPS", 50),
        "exit_slippage_bps": env_float("PAPER_EXIT_SLIPPAGE_BPS", 100),
        "max_hold_hours": env_float("PAPER_MAX_HOLD_HOURS", 12),
        "min_liquidity_factor": env_float("PAPER_MIN_LIQUIDITY_FACTOR", 0.5),
        "take_profit_pct": env_float("PAPER_TAKE_PROFIT_PCT", 0.10),
        "stop_loss_pct": env_float("PAPER_STOP_LOSS_PCT", 0.05),
        "entry_sol": env_float("PAPER_ENTRY_SOL", 1.0),
        "unit_sol_usd": env_float("PAPER_UNIT_SOL_USD_FALLBACK", 150.0),
    }
    for key in cfg:
        if key in settings:
            cfg[key] = settings[key]
    cfg["risk_per_trade"] = max(0.005, min(0.10, float(cfg["risk_per_trade"])))
    cfg["max_position_pct"] = max(0.05, min(1.0, float(cfg["max_position_pct"])))
    cfg["max_positions"] = max(1, min(10, int(cfg["max_positions"])))
    cfg["take_profit_pct"] = max(0.01, min(1.0, float(cfg["take_profit_pct"])))
    cfg["stop_loss_pct"] = max(0.01, min(0.50, float(cfg["stop_loss_pct"])))
    cfg["entry_sol"] = max(0.1, min(10.0, float(cfg["entry_sol"])))
    cfg["unit_sol_usd"] = max(1.0, min(10000.0, float(cfg["unit_sol_usd"])))
    cfg["fee_bps"] = max(0, min(500, float(cfg["fee_bps"])))
    cfg["entry_slippage_bps"] = max(0, min(1000, float(cfg["entry_slippage_bps"])))
    cfg["exit_slippage_bps"] = max(0, min(2000, float(cfg["exit_slippage_bps"])))
    cfg["max_hold_hours"] = max(1, min(168, float(cfg["max_hold_hours"])))
    cfg["min_liquidity_factor"] = max(0.1, min(1.0, float(cfg["min_liquidity_factor"])) )
    return cfg


def fetch_sol_usd_price(fallback: float) -> float:
    """Read a current wrapped-SOL USD quote; fallback only if the public endpoint fails."""
    try:
        data = get_json(f"{DEX_BASE}/tokens/v1/solana/{SOLANA_NATIVE}", timeout=15)
        if isinstance(data, list):
            for pair in data:
                base = (pair.get("baseToken") or {}).get("address")
                price = finite_number(pair.get("priceUsd"))
                if base == SOLANA_NATIVE and price > 0:
                    return price
    except Exception as exc:
        LOG.warning("SOL price lookup failed; using fallback: %s", exc)
    return fallback


def buy_momentum(candidate: dict[str, Any]) -> bool:
    return int(candidate.get("buys_h1") or 0) > int(candidate.get("sells_h1") or 0)


def paper_equity(state: dict[str, Any], prices: dict[str, float] | None = None) -> float:
    prices = prices or {}
    equity = finite_number(state.get("cash"))
    for mint, pos in (state.get("positions") or {}).items():
        price = prices.get(mint, finite_number(pos.get("last_price"), finite_number(pos.get("entry_price"))))
        equity += finite_number(pos.get("quantity")) * price
    return equity


def paper_ledger(event: dict[str, Any]) -> None:
    PAPER_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PAPER_LEDGER_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def paper_close(state: dict[str, Any], mint: str, price: float, fraction: float, reason: str, cfg: dict[str, Any], now: str) -> dict[str, Any] | None:
    pos = (state.get("positions") or {}).get(mint)
    if not pos or price <= 0:
        return None
    fraction = max(0.0, min(1.0, fraction))
    quantity = finite_number(pos.get("quantity")) * fraction
    if quantity <= 0:
        return None
    exec_price = price * (1.0 - cfg["exit_slippage_bps"] / 10000.0)
    gross = quantity * exec_price
    fee = gross * cfg["fee_bps"] / 10000.0
    cost = quantity * finite_number(pos.get("cost_per_unit"), finite_number(pos.get("entry_price")))
    pnl = gross - fee - cost
    state["cash"] = finite_number(state.get("cash")) + gross - fee
    state["realized_pnl"] = finite_number(state.get("realized_pnl")) + pnl
    state["fees_paid"] = finite_number(state.get("fees_paid")) + fee
    pos["quantity"] = max(0.0, finite_number(pos.get("quantity")) - quantity)
    pos["last_price"] = price
    event = {"type": "close", "mint": mint, "symbol": pos.get("symbol"), "reason": reason, "price": price, "execution_price": exec_price, "quantity": quantity, "pnl": pnl, "pnl_pct": (100.0 * pnl / cost) if cost else 0.0, "fee": fee, "time": now}
    state.setdefault("trades", []).append(event)
    paper_ledger(event)
    if pos["quantity"] <= 1e-12:
        state["positions"].pop(mint, None)
        if pnl >= 0:
            state["wins"] = int(state.get("wins", 0)) + 1
        else:
            state["losses"] = int(state.get("losses", 0)) + 1
    return event


def paper_open(state: dict[str, Any], candidate: dict[str, Any], cfg: dict[str, Any], now: str) -> dict[str, Any] | None:
    mint = str(candidate.get("mint") or "")
    price = finite_number(candidate.get("price_usd"))
    if not mint or price <= 0 or mint in state.get("positions", {}):
        return None
    if not buy_momentum(candidate):
        return None
    if len(state.get("positions") or {}) >= cfg["max_positions"]:
        return None
    equity = paper_equity(state)
    target_notional = cfg["entry_sol"] * finite_number(cfg.get("sol_usd"), cfg["unit_sol_usd"])
    max_notional = equity * cfg["max_position_pct"]
    notional = min(target_notional, max_notional)
    exec_price = price * (1.0 + cfg["entry_slippage_bps"] / 10000.0)
    quantity = notional / exec_price
    fee = notional * cfg["fee_bps"] / 10000.0
    total = notional + fee
    if total > finite_number(state.get("cash")):
        LOG.info("paper skip: 1 SOL position costs %.4f but cash is %.4f", total, finite_number(state.get("cash")))
        return None
    state["cash"] = finite_number(state.get("cash")) - total
    state["fees_paid"] = finite_number(state.get("fees_paid")) + fee
    cost_per_unit = total / quantity
    pos = {
        "mint": mint,
        "symbol": candidate.get("symbol"),
        "name": candidate.get("name"),
        "quantity": quantity,
        "entry_sol": cfg["entry_sol"],
        "sol_usd": cfg.get("sol_usd"),
        "entry_price": price,
        "execution_entry_price": exec_price,
        "cost_per_unit": cost_per_unit,
        "notional": notional,
        "stop_price": price * (1.0 - cfg["stop_loss_pct"]),
        "take_profit_price": price * (1.0 + cfg["take_profit_pct"]),
        "opened_at": now,
        "last_price": price,
        "highest_price": price,
        "buys_h1": candidate.get("buys_h1"),
        "sells_h1": candidate.get("sells_h1"),
    }
    state.setdefault("positions", {})[mint] = pos
    event = {"type": "open", "mint": mint, "symbol": candidate.get("symbol"), "price": price, "execution_price": exec_price, "quantity": quantity, "notional": notional, "entry_sol": cfg["entry_sol"], "sol_usd": cfg.get("sol_usd"), "fee": fee, "time": now}
    state.setdefault("trades", []).append(event)
    paper_ledger(event)
    return event


def paper_process_results(results: list[dict[str, Any]], signal_threshold: float, min_liquidity: float) -> list[dict[str, Any]]:
    state = load_paper_state()
    cfg = paper_cfg()
    cfg["sol_usd"] = fetch_sol_usd_price(cfg["unit_sol_usd"])
    if not state.get("enabled", True):
        return []
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    by_mint = {str(item.get("mint")): item for item in results if item.get("mint")}
    events: list[dict[str, Any]] = []
    closed_this_cycle: set[str] = set()
    for mint, pos in list((state.get("positions") or {}).items()):
        candidate = by_mint.get(mint)
        price = finite_number((candidate or {}).get("price_usd"), finite_number(pos.get("last_price")))
        liquidity = finite_number((candidate or {}).get("liquidity_usd"), 0.0)
        pos["last_price"] = price
        pos["highest_price"] = max(finite_number(pos.get("highest_price")), price)
        opened = datetime.fromisoformat(str(pos.get("opened_at")).replace("Z", "+00:00"))
        age = max(0.0, (now_dt - opened).total_seconds() / 3600.0)
        if price <= finite_number(pos.get("stop_price")):
            event = paper_close(state, mint, price, 1.0, "STOP_LOSS", cfg, now)
            if event:
                events.append(event)
                closed_this_cycle.add(mint)
        elif price >= finite_number(pos.get("take_profit_price")):
            event = paper_close(state, mint, price, 1.0, "TAKE_PROFIT_10PCT", cfg, now)
            if event:
                events.append(event)
                closed_this_cycle.add(mint)
        elif age >= cfg["max_hold_hours"]:
            event = paper_close(state, mint, price, 1.0, "TIME_EXIT", cfg, now)
            if event:
                events.append(event)
                closed_this_cycle.add(mint)
        elif candidate and liquidity > 0 and liquidity < min_liquidity * cfg["min_liquidity_factor"]:
            event = paper_close(state, mint, price, 1.0, "LIQUIDITY_EXIT", cfg, now)
            if event:
                events.append(event)
                closed_this_cycle.add(mint)
    for candidate in results:
        assessment = candidate.get("assessment") or {}
        if candidate.get("mint") not in closed_this_cycle and candidate.get("hard_gate_passed") and assessment.get("action") == "SIGNAL" and finite_number(candidate.get("raw_score")) >= signal_threshold and buy_momentum(candidate):
            event = paper_open(state, candidate, cfg, now)
            if event: events.append(event)
    state["last_update"] = now
    state["trades"] = state.get("trades", [])[-2000:]
    save_paper_state(state)
    return events


def pnl_marker(value: float) -> str:
    if value > 1e-12:
        return "🟩 ربح"
    if value < -1e-12:
        return "🟥 خسارة"
    return "⬜ تعادل"


def format_paper_event(event: dict[str, Any]) -> str:
    symbol = html.escape(str(event.get("symbol", "?")))
    if event.get("type") == "open":
        return (
            "<b>محاكاة — فتح مركز ورقي</b>\n"
            f"<b>${symbol}</b> | القيمة: <b>{fmt_usd(event.get('notional'))}</b>\n"
            f"سعر السوق: {fmt_usd(event.get('price'))} | التنفيذ المحاكى: {fmt_usd(event.get('execution_price'))}\n"
            f"الرسوم: {fmt_usd(event.get('fee'))}\n"
            "لا أموال حقيقية مستخدمة."
        )
    pnl = finite_number(event.get("pnl"))
    marker = pnl_marker(pnl)
    return (
        f"<b>محاكاة — إغلاق مركز ورقي</b>\n"
        f"<b>${symbol}</b> | السبب: <code>{html.escape(str(event.get('reason', 'UNKNOWN')))}</code>\n"
        f"سعر السوق: {fmt_usd(event.get('price'))}\n"
        f"{marker}: <b>{fmt_usd(pnl)}</b> ({finite_number(event.get('pnl_pct')):+.2f}%)\n"
        f"الرسوم: {fmt_usd(event.get('fee'))}\n"
        "النتيجة افتراضية وليست أداءً حقيقياً."
    )


def persist_paper_setting(key: str, raw: str) -> str:
    allowed = {"risk_per_trade", "max_position_pct", "max_positions", "fee_bps", "entry_slippage_bps", "exit_slippage_bps", "max_hold_hours", "take_profit_pct", "stop_loss_pct", "entry_sol", "unit_sol_usd"}
    if key not in allowed:
        return "إعداد محاكاة غير مسموح. راجع /help."
    try:
        value = float(raw)
        limits = {
            "risk_per_trade": (0.005, 0.10), "max_position_pct": (0.05, 1.0),
            "max_positions": (1, 10), "fee_bps": (0, 500),
            "entry_slippage_bps": (0, 1000), "exit_slippage_bps": (0, 2000),
            "max_hold_hours": (1, 168), "take_profit_pct": (0.01, 1.0),
            "stop_loss_pct": (0.01, 0.50), "entry_sol": (0.1, 10.0),
            "unit_sol_usd": (1.0, 10000.0),
        }
        low, high = limits[key]
        if not low <= value <= high:
            raise ValueError
        if key == "max_positions":
            value = int(value)
    except ValueError:
        return "قيمة غير صالحة أو خارج الحدود الآمنة للمحاكاة."
    state = load_paper_state()
    state.setdefault("settings", {})[key] = value
    save_paper_state(state)
    return f"تم تعديل إعداد المحاكاة {key} إلى {value}."


def reset_paper_state() -> None:
    save_paper_state(paper_default_state())
    if PAPER_LEDGER_FILE.exists():
        PAPER_LEDGER_FILE.unlink()


def paper_position_pnl(pos: dict[str, Any], cfg: dict[str, Any]) -> tuple[float, float, float]:
    price = finite_number(pos.get("last_price"))
    quantity = finite_number(pos.get("quantity"))
    mark_price = price * (1.0 - cfg["exit_slippage_bps"] / 10000.0)
    net_value = quantity * mark_price * (1.0 - cfg["fee_bps"] / 10000.0)
    cost_basis = quantity * finite_number(pos.get("cost_per_unit"), finite_number(pos.get("entry_price")))
    pnl = net_value - cost_basis
    pct = 100.0 * pnl / cost_basis if cost_basis else 0.0
    return pnl, pct, net_value


def paper_status() -> str:
    state = load_paper_state()
    cfg = paper_cfg()
    prices = {mint: finite_number(pos.get("last_price")) for mint, pos in (state.get("positions") or {}).items()}
    equity = paper_equity(state, prices)
    initial = finite_number(state.get("initial_balance"), 200.0)
    pnl = equity - initial
    trades = [x for x in state.get("trades", []) if x.get("type") == "close"]
    winrate = (100.0 * int(state.get("wins", 0)) / len(trades)) if trades else 0.0
    lines = [
        "<b>محاكاة التداول الورقي — لا أموال حقيقية</b>",
        f"الحالة: {'مفعّلة' if state.get('enabled', True) else 'متوقفة'}",
        f"الرصيد الابتدائي: {fmt_usd(initial)} | القيمة الحالية: {fmt_usd(equity)}",
        f"{pnl_marker(pnl)}: <b>{fmt_usd(pnl)}</b>",
        f"النقد المتاح: {fmt_usd(state.get('cash'))} | الربح المحقق: {fmt_usd(state.get('realized_pnl'))}",
        f"الرسوم: {fmt_usd(state.get('fees_paid'))} | صفقات مغلقة: {len(trades)} | نسبة الفوز: {winrate:.1f}%",
    ]
    if not (state.get("positions") or {}):
        lines.append("لا توجد مراكز مفتوحة حالياً.")
    for pos in (state.get("positions") or {}).values():
        open_pnl, open_pct, net_value = paper_position_pnl(pos, cfg)
        lines.append(
            f"\n<b>مركز مفتوح ${html.escape(str(pos.get('symbol', '?')))}</b>\n"
            f"القيمة عند التصفية المحاكاة: {fmt_usd(net_value)}\n"
            f"{pnl_marker(open_pnl)}: <b>{fmt_usd(open_pnl)}</b> ({open_pct:+.2f}%)\n"
            f"الدخول: {fmt_usd(pos.get('entry_price'))} | الحالي: {fmt_usd(pos.get('last_price'))}\n"
            f"وقف 5%: {fmt_usd(pos.get('stop_price'))} | هدف 10%: {fmt_usd(pos.get('take_profit_price'))}"
        )
    return "\n".join(lines)


def active_trades_report() -> str:
    state = load_paper_state()
    cfg = paper_cfg()
    positions = list((state.get("positions") or {}).values())
    if not positions:
        return "الصفقات النشطة: لا توجد مراكز مفتوحة حالياً."
    lines = ["<b>الصفقات النشطة — محاكاة فقط</b>"]
    for pos in positions:
        pnl, pct, value = paper_position_pnl(pos, cfg)
        marker = pnl_marker(pnl)
        lines.append(
            f"\n<b>${html.escape(str(pos.get('symbol', '?')))}</b> | حجم الدخول: {finite_number(pos.get('entry_sol'), 1.0):.2f} SOL\n"
            f"{marker}: <b>{fmt_usd(pnl)}</b> ({pct:+.2f}%) | القيمة: {fmt_usd(value)}\n"
            f"شراء/بيع 1س: {pos.get('buys_h1', 0)}/{pos.get('sells_h1', 0)}\n"
            f"دخول: {fmt_usd(pos.get('entry_price'))} | الحالي: {fmt_usd(pos.get('last_price'))}\n"
            f"وقف: {fmt_usd(pos.get('stop_price'))} | هدف: {fmt_usd(pos.get('take_profit_price'))}"
        )
    return "\n".join(lines)


def balance_report() -> str:
    state = load_paper_state()
    prices = {mint: finite_number(pos.get("last_price")) for mint, pos in (state.get("positions") or {}).items()}
    equity = paper_equity(state, prices)
    initial = finite_number(state.get("initial_balance"), 200.0)
    pnl = equity - initial
    return (
        "<b>الرصيد المحاكى</b>\n"
        f"القيمة الحالية: <b>{fmt_usd(equity)}</b>\n"
        f"النقد المتاح: {fmt_usd(state.get('cash'))}\n"
        f"{pnl_marker(pnl)}: <b>{fmt_usd(pnl)}</b>\n"
        f"الربح المحقق: {fmt_usd(state.get('realized_pnl'))}\n"
        f"الرسوم: {fmt_usd(state.get('fees_paid'))}\n"
        "لا توجد أموال حقيقية."
    )


def paper_report() -> str:
    state = load_paper_state()
    closes = [x for x in state.get("trades", []) if x.get("type") == "close"]
    wins = [finite_number(x.get("pnl")) for x in closes if finite_number(x.get("pnl")) > 0]
    losses = [finite_number(x.get("pnl")) for x in closes if finite_number(x.get("pnl")) < 0]
    total_pnl = finite_number(state.get("realized_pnl"))
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)
    win_rate = 100.0 * len(wins) / len(closes) if closes else 0.0
    failure_rate = 100.0 - win_rate if closes else 0.0
    cfg = paper_cfg()
    positions = state.get("positions") or {}
    equity = paper_equity(state)
    pf_text = "∞" if math.isinf(profit_factor) else f"{profit_factor:.2f}"
    return (
        "<b>تقرير /stock-analysis — محاكاة التداول</b>\n"
        f"إجمالي الصفقات المغلقة: <b>{len(closes)}</b>\n"
        f"الناجحة: <b>{len(wins)}</b> | الفاشلة: <b>{len(losses)}</b>\n"
        f"نسبة النجاح: <b>{win_rate:.2f}%</b> | نسبة الفشل: {failure_rate:.2f}%\n"
        f"إجمالي الربح المحقق: {fmt_usd(gross_profit)} | إجمالي الخسائر: {fmt_usd(gross_loss)}\n"
        f"صافي الربح/الخسارة المحققة: <b>{fmt_usd(total_pnl)}</b>\n"
        f"Profit Factor: {pf_text} | متوسط الربح: {fmt_usd(sum(wins) / len(wins) if wins else 0)} | متوسط الخسارة: {fmt_usd(sum(losses) / len(losses) if losses else 0)}\n"
        f"القيمة الحالية: <b>{fmt_usd(equity)}</b> | المراكز المفتوحة: {len(positions)}\n"
        f"الإعداد: {cfg['entry_sol']:.2f} SOL | هدف +{cfg['take_profit_pct'] * 100:.1f}% | وقف -{cfg['stop_loss_pct'] * 100:.1f}%\n"
        f"الرسوم المدفوعة: {fmt_usd(state.get('fees_paid'))}\n"
        "هذا تقرير محاكاة ولا يمثل تداولاً حقيقياً أو ضماناً للربح."
    )


def send_telegram(text: str, cfg: dict[str, Any], *, parse_mode: str | None = None) -> dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID مطلوبان")
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4090]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return post_json(f"{TELEGRAM_BASE}/bot{token}/sendMessage", payload, timeout=30)


def scan_once(*, send: bool = False, dry_run: bool = False) -> list[dict[str, Any]]:
    cfg = runtime_cfg()
    profiles = fetch_profiles()
    pairs_by_token = fetch_pairs([p["tokenAddress"] for p in profiles])
    ranked: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for profile in profiles:
        pair = choose_pair(pairs_by_token.get(profile["tokenAddress"], []))
        if not pair:
            continue
        rough = nested_number(pair.get("liquidity"), "usd") + nested_number(pair.get("volume"), "h1")
        ranked.append((rough, profile, pair, {}))
    ranked.sort(key=lambda x: x[0], reverse=True)
    results: list[dict[str, Any]] = []
    for _, profile, pair, _ in ranked[: cfg["max_candidates"]]:
        try:
            report = rug_report(profile["tokenAddress"])
            candidate = normalize_candidate(profile, pair, report, cfg)
            if candidate["hard_gate_passed"]:
                assessment = ai_assess(candidate, cfg)
            else:
                assessment = {
                    "action": "AVOID",
                    "confidence": int(max(0, min(100, 100 - candidate["raw_score"]))),
                    "thesis": "تم رفض المرشح قبل الذكاء الاصطناعي بسبب بوابة المخاطر الأساسية.",
                    "risks": candidate.get("gate_reasons", []) or ["فشل بوابة المخاطر"],
                    "invalidation": "لا توجد إشارة صالحة؛ يجب إعادة الفحص من الصفر بعد تغير البيانات.",
                    "entry_plan": "لا دخول.",
                    "exit_plan": "لا توجد صفقة.",
                    "rationale": "قرار حتمي للحماية، وليس توقعاً للسعر.",
                }
            candidate["assessment"] = assessment
            candidate["reference_time_utc"] = datetime.now(timezone.utc).isoformat()
            results.append(candidate)
        except Exception as exc:
            LOG.warning("candidate failed %s: %s", profile.get("tokenAddress"), exc)
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS / f"scan_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    state = load_state()
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state.setdefault("sent_mints", {})
    for candidate in results:
        assessment = candidate.get("assessment") or {}
        should_send = (
            send
            and not dry_run
            and candidate.get("hard_gate_passed")
            and assessment.get("action") == "SIGNAL"
            and candidate.get("raw_score", 0) >= cfg["signal_threshold"]
            and candidate.get("mint") not in state["sent_mints"]
        )
        if should_send:
            send_telegram(format_signal(candidate, assessment), cfg)
            state["sent_mints"][candidate["mint"]] = datetime.now(timezone.utc).isoformat()
    # Keep state bounded while preserving recent deduplication.
    if len(state["sent_mints"]) > 1000:
        recent = sorted(state["sent_mints"].items(), key=lambda kv: kv[1], reverse=True)[:500]
        state["sent_mints"] = dict(recent)
    if not dry_run:
        save_state(state)
        try:
            paper_events = paper_process_results(results, cfg["signal_threshold"], cfg["min_liquidity"])
            for event in paper_events:
                LOG.info("paper event: type=%s symbol=%s reason=%s pnl=%s", event.get("type"), event.get("symbol"), event.get("reason"), event.get("pnl"))
                try:
                    send_telegram(format_paper_event(event), cfg, parse_mode="HTML")
                except Exception as exc:
                    LOG.warning("paper event notification failed: %s", exc)
        except Exception:
            LOG.exception("paper engine failed")
    LOG.info("scan complete: profiles=%s candidates=%s report=%s", len(profiles), len(results), report_path)
    return results


def telegram_test() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("بيانات تيليجرام ناقصة")
    me = get_json(f"{TELEGRAM_BASE}/bot{token}/getMe")
    print(json.dumps({"bot_ok": True, "bot": me.get("result", {}).get("username")}, ensure_ascii=False))
    send_telegram("اختبار اتصال ناجح: بوت إشارات سولانا يعمل في الوضع التجريبي ولا ينفذ صفقات.", {})
    print(json.dumps({"message_sent": True, "chat_id": chat_id}, ensure_ascii=False))


def runtime_cfg() -> dict[str, Any]:
    state = load_state()
    settings = state.get("settings") or {}
    cfg = {
        "min_liquidity": env_float("MIN_LIQUIDITY_USD", 5000),
        "min_h1_volume": env_float("MIN_H1_VOLUME_USD", 5000),
        "max_age_hours": env_float("MAX_SIGNAL_AGE_HOURS", 48),
        "max_top_holder_pct": env_float("MAX_TOP_HOLDER_PCT", 40),
        "max_candidates": env_int("MAX_CANDIDATES", 8),
        "signal_threshold": env_float("SIGNAL_THRESHOLD", 65),
        "ai_enabled": os.getenv("AI_ENABLED", "true").lower() in {"1", "true", "yes"},
    }
    for key in ("min_liquidity", "min_h1_volume", "max_age_hours", "max_top_holder_pct", "max_candidates", "signal_threshold"):
        if key in settings:
            cfg[key] = settings[key]
    return cfg


def latest_results() -> list[dict[str, Any]]:
    reports = sorted(REPORTS.glob("scan_*.json"), reverse=True)
    if not reports:
        return []
    try:
        data = json.loads(reports[0].read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def authorized_update(update: dict[str, Any]) -> bool:
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    user_id = str((message.get("from") or {}).get("id", ""))
    allowed = str(os.getenv("TELEGRAM_ADMIN_ID") or os.getenv("TELEGRAM_CHAT_ID") or "")
    return bool(allowed) and (chat_id == allowed or user_id == allowed)


def admin_chat_id() -> str:
    return os.getenv("TELEGRAM_ADMIN_ID") or os.getenv("TELEGRAM_CHAT_ID") or ""


def telegram_api(method: str, payload: dict[str, Any] | None = None, *, timeout: int = 30) -> Any:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN مطلوب")
    url = f"{TELEGRAM_BASE}/bot{token}/{method}"
    if payload is None:
        return get_json(url, timeout=timeout)
    return post_json(url, payload, timeout=timeout)


def set_telegram_commands() -> None:
    commands = [
        {"command": "help", "description": "عرض أوامر التحكم"},
        {"command": "status", "description": "عرض حالة البوت والإعدادات"},
        {"command": "start_bot", "description": "تشغيل البوت والمحاكاة"},
        {"command": "stop_bot", "description": "إيقاف البوت والمحاكاة"},
        {"command": "start_analysis", "description": "بدء تحليل فوري"},
        {"command": "active_trades", "description": "الصفقات النشطة"},
        {"command": "balance", "description": "الرصيد المحاكى"},
        {"command": "stock_analysis", "description": "تقرير أداء كامل"},
        {"command": "scan", "description": "تشغيل فحص فوري"},
        {"command": "pause", "description": "إيقاف الفحص التلقائي"},
        {"command": "resume", "description": "استئناف الفحص التلقائي"},
        {"command": "stop", "description": "إيقاف عملية البوت"},
        {"command": "alerts_on", "description": "تفعيل إرسال الإشارات"},
        {"command": "alerts_off", "description": "تعطيل إرسال الإشارات"},
        {"command": "last", "description": "عرض آخر النتائج"},
        {"command": "set", "description": "تعديل حد أو إعداد"},
        {"command": "paper_status", "description": "حالة المحاكاة الورقية"},
        {"command": "paper_pause", "description": "إيقاف المحاكاة"},
        {"command": "paper_resume", "description": "استئناف المحاكاة"},
        {"command": "paper_reset", "description": "إعادة ضبط رصيد المحاكاة"},
        {"command": "paper_set", "description": "تعديل إعدادات المحاكاة"},
    ]
    telegram_api("setMyCommands", {"commands": commands}, timeout=30)


def control_help() -> str:
    return (
        "أوامر التحكم الإدارية:\n"
        "/status — الحالة والإعدادات الحالية\n"
        "/start_bot — تشغيل البوت والمحاكاة\n"
        "/stop_bot — إيقاف البوت والمحاكاة مؤقتاً\n"
        "/start_analysis — بدء تحليل فوري\n"
        "/active_trades — عرض الصفقات المفتوحة وأدائها\n"
        "/balance — عرض الرصيد والقيمة الحالية\n"
        "/stock_analysis — تقرير النجاح والفشل وإجمالي الصفقات\n"
        "/scan — فحص فوري دون إرسال إشارات منفصلة\n"
        "/pause — إيقاف الفحص التلقائي\n"
        "/resume — استئناف الفحص التلقائي\n"
        "/stop — إيقاف عملية البوت بالكامل\n"
        "/alerts_on أو /alerts_off — تشغيل أو منع الإشعارات\n"
        "/last — آخر نتائج الفحص\n"
        "/set min_liquidity 10000\n"
        "/set min_h1_volume 10000\n"
        "/set signal_threshold 70\n"
        "/set max_age_hours 24\n"
        "/set max_candidates 8\n"
        "/paper_status — حالة رصيد 200 USDC الافتراضي\n"
        "/paper_pause أو /paper_resume\n"
        "/paper_reset — تصفير المحاكاة\n"
        "/paper_set risk_per_trade 0.03\n"
        "/paper_set max_positions 4\n"
        "/paper_set fee_bps 30\n"
        "/paper_set entry_sol 1\n"
        "/paper_set take_profit_pct 0.10\n"
        "/paper_set stop_loss_pct 0.05\n\n"
        "هذا البوت لا ينفذ صفقات ولا يملك مفتاح محفظة."
    )


def format_status() -> str:
    state = load_state()
    cfg = runtime_cfg()
    last_scan = state.get("last_scan") or "لم يبدأ بعد"
    sent_count = len(state.get("sent_mints") or {})
    enabled = "مفعّل" if state.get("enabled", True) else "متوقف"
    alerts = "مفعّلة" if state.get("alerts_enabled", True) else "متوقفة"
    return (
        f"حالة البوت: {enabled}\n"
        f"الإشعارات: {alerts}\n"
        f"آخر فحص UTC: {last_scan}\n"
        f"إشارات أُرسلت سابقاً: {sent_count}\n"
        f"السيولة الدنيا: ${cfg['min_liquidity']:,.0f}\n"
        f"حجم الساعة الأدنى: ${cfg['min_h1_volume']:,.0f}\n"
        f"حد الإشارة: {cfg['signal_threshold']:.0f}/100\n"
        f"نافذة العمر: {cfg['max_age_hours']:.0f} ساعة\n"
        f"عدد المرشحين: {cfg['max_candidates']}"
    )


def persist_setting(key: str, raw: str) -> str:
    allowed = {"min_liquidity", "min_h1_volume", "max_age_hours", "max_top_holder_pct", "max_candidates", "signal_threshold"}
    if key not in allowed:
        return "الإعداد غير مسموح. راجع /help."
    try:
        value = float(raw)
        if key == "max_candidates":
            value = int(value)
            if not 1 <= value <= 20:
                raise ValueError
        elif key in {"min_liquidity", "min_h1_volume"} and not 0 < value <= 10_000_000:
            raise ValueError
        elif key == "signal_threshold" and not 0 <= value <= 100:
            raise ValueError
        elif key == "max_age_hours" and not 1 <= value <= 168:
            raise ValueError
        elif key == "max_top_holder_pct" and not 1 <= value <= 100:
            raise ValueError
    except ValueError:
        return "قيمة غير صالحة أو خارج الحدود الآمنة."
    state = load_state()
    state.setdefault("settings", {})[key] = value
    save_state(state)
    return f"تم تعديل {key} إلى {value}."


def compact_last_results() -> str:
    results = latest_results()
    if not results:
        return "لا توجد نتائج محفوظة بعد."
    lines = ["آخر نتائج الفحص:"]
    for item in results[:5]:
        assessment = item.get("assessment") or {}
        lines.append(
            f"{assessment.get('action', 'UNKNOWN')} — {item.get('name', '?')} (${item.get('symbol', '?')}) "
            f"| الدرجة {finite_number(item.get('raw_score')):.1f} | السيولة ${finite_number(item.get('liquidity_usd')):,.0f}"
        )
    return "\n".join(lines)


def guarded_scan_once(*, send: bool = False, dry_run: bool = False) -> list[dict[str, Any]]:
    if not SCAN_LOCK.acquire(blocking=False):
        raise RuntimeError("يوجد فحص جارٍ حالياً")
    try:
        return scan_once(send=send, dry_run=dry_run)
    finally:
        SCAN_LOCK.release()


def handle_command(update: dict[str, Any]) -> None:
    if not authorized_update(update):
        return
    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return
    command, _, args = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    chat_id = str((message.get("chat") or {}).get("id") or admin_chat_id())
    if command in {"/start", "/help"}:
        reply = control_help()
    elif command == "/status":
        reply = format_status()
    elif command == "/start_bot":
        state = load_state(); state["enabled"] = True; save_state(state)
        paper = load_paper_state(); paper["enabled"] = True; save_paper_state(paper)
        reply = "تم تشغيل البوت والمحاكاة الورقية."
    elif command == "/stop_bot":
        state = load_state(); state["enabled"] = False; save_state(state)
        paper = load_paper_state(); paper["enabled"] = False; save_paper_state(paper)
        reply = "تم إيقاف البوت والمحاكاة مؤقتاً. استخدم /start_bot للبدء."
    elif command == "/start_analysis":
        state = load_state(); state["enabled"] = True; save_state(state)
        reply = "بدأ التحليل الآن؛ سيصل ملخص الدورة بعد اكتمالها."
        telegram_api("sendMessage", {"chat_id": chat_id, "text": reply}, timeout=30)
        def run_manual_analysis() -> None:
            try:
                guarded_scan_once(send=False, dry_run=False)
                telegram_api("sendMessage", {"chat_id": chat_id, "text": "اكتمل التحليل.\n" + compact_last_results()}, timeout=30)
            except Exception as exc:
                telegram_api("sendMessage", {"chat_id": chat_id, "text": f"تعذر إكمال التحليل: {str(exc)[:300]}"}, timeout=30)
        threading.Thread(target=run_manual_analysis, daemon=True).start()
        return
    elif command == "/active_trades":
        reply = active_trades_report()
    elif command == "/balance":
        reply = balance_report()
    elif command in {"/stock_analysis", "/stock-analysis"}:
        reply = paper_report()
    elif command == "/pause":
        state = load_state(); state["enabled"] = False; save_state(state)
        reply = "تم إيقاف الفحص التلقائي. استخدم /resume لاستئنافه."
    elif command == "/resume":
        state = load_state(); state["enabled"] = True; save_state(state)
        reply = "تم استئناف الفحص التلقائي."
    elif command == "/stop":
        STOP_EVENT.set()
        reply = "تم استلام أمر الإيقاف. سيُنهي البوت دورة الفحص الحالية ثم يتوقف."
    elif command == "/alerts_off":
        state = load_state(); state["alerts_enabled"] = False; save_state(state)
        reply = "تم تعطيل إرسال الإشارات. سيستمر الفحص دون إرسال تنبيهات جديدة."
    elif command == "/alerts_on":
        state = load_state(); state["alerts_enabled"] = True; save_state(state)
        reply = "تم تفعيل إرسال الإشارات المؤهلة."
    elif command == "/last":
        reply = compact_last_results()
    elif command == "/paper_status":
        reply = paper_status()
    elif command == "/paper_pause":
        state = load_paper_state(); state["enabled"] = False; save_paper_state(state)
        reply = "تم إيقاف محرك المحاكاة الورقية."
    elif command == "/paper_resume":
        state = load_paper_state(); state["enabled"] = True; save_paper_state(state)
        reply = "تم استئناف محرك المحاكاة الورقية."
    elif command == "/paper_reset":
        reset_paper_state()
        reply = "تمت إعادة ضبط المحاكاة إلى 200 USDC افتراضية وحُذف سجلها."
    elif command == "/paper_set":
        parts = args.split()
        reply = persist_paper_setting(parts[0], parts[1]) if len(parts) == 2 else "الصيغة: /paper_set اسم_الإعداد القيمة"
    elif command == "/set":
        parts = args.split()
        reply = persist_setting(parts[0], parts[1]) if len(parts) == 2 else "الصيغة: /set اسم_الإعداد القيمة"
    elif command == "/scan":
        reply = "بدأت فحصاً فورياً. سأرسل ملخص النتائج بعد اكتماله."
        telegram_api("sendMessage", {"chat_id": chat_id, "text": reply}, timeout=30)
        try:
            guarded_scan_once(send=False, dry_run=False)
            reply = "اكتمل الفحص الفوري.\n" + compact_last_results()
        except Exception as exc:
            LOG.exception("manual scan failed")
            reply = f"فشل الفحص الفوري: {str(exc)[:300]}"
    else:
        reply = "أمر غير معروف. استخدم /help."
    final_payload: dict[str, Any] = {"chat_id": chat_id, "text": reply[:4090]}
    if command in {"/paper_status", "/active_trades", "/balance", "/stock_analysis", "/stock-analysis"}:
        final_payload["parse_mode"] = "HTML"
    telegram_api("sendMessage", final_payload, timeout=30)


def telegram_poll_loop() -> None:
    offset = 0
    try:
        set_telegram_commands()
    except Exception as exc:
        LOG.warning("setMyCommands failed: %s", exc)
    while not STOP_EVENT.is_set():
        try:
            data = telegram_api("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": ["message"]}, timeout=30)
            updates = (data or {}).get("result", []) if isinstance(data, dict) else []
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                threading.Thread(target=handle_command, args=(update,), daemon=True).start()
        except Exception as exc:
            LOG.warning("Telegram polling failed: %s", exc)
            time.sleep(10)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Solana meme-token signal bot")
    parser.add_argument("--once", action="store_true", help="run one scan")
    parser.add_argument("--dry-run", action="store_true", help="never send or save dedupe state")
    parser.add_argument("--send", action="store_true", help="send eligible signals to Telegram")
    parser.add_argument("--telegram-test", action="store_true", help="validate bot token and send a test message")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    if args.telegram_test:
        telegram_test()
        return 0
    if args.once:
        results = guarded_scan_once(send=args.send, dry_run=args.dry_run)
        for item in results:
            print(format_signal(item, item.get("assessment") or {}))
            print("\n" + "=" * 80 + "\n")
        return 0
    poll_seconds = max(120, env_int("POLL_SECONDS", 120))
    threading.Thread(target=telegram_poll_loop, daemon=True, name="telegram-control").start()
    while not STOP_EVENT.is_set():
        try:
            state = load_state()
            if state.get("enabled", True):
                guarded_scan_once(send=state.get("alerts_enabled", True))
            else:
                LOG.info("automatic scan paused")
        except KeyboardInterrupt:
            STOP_EVENT.set()
            return 0
        except Exception:
            LOG.exception("scan loop failed")
        STOP_EVENT.wait(poll_seconds)
    LOG.info("bot stopped by Telegram command or shutdown signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
