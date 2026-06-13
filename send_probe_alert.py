"""
send_probe_alert.py — Probe Candidate Alert Emailer

Checks for tickers that cleared all discovery thresholds this week
(recommendation = 'probe_candidate') and sends a red-alert HTML email
to johnmorgan.tlh@gmail.com.

If no probe candidates exist this week → exits silently, no email.

Schedule: Windows Task Scheduler, 07:00 daily.
Manual:   python send_probe_alert.py
"""
from __future__ import annotations

import json
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

# Reuse the same Gmail credentials as the podcast digest
_CONFIG_PATH = Path(r"C:\Users\noleg\Desktop\Claude\Projects\Podcasts Pull\digest_config.json")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _current_week_key() -> str:
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _load_config() -> dict:
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("app_password") in ("", "YOUR_APP_PASSWORD_HERE"):
        raise ValueError("Gmail app password not set in digest_config.json")
    return cfg


def _fetch_probe_candidates(week_key: str) -> list:
    """Query DB for probe_candidate rows this week, sorted by total_score desc."""
    import os, sys
    # Ensure the src package is importable from the Stocks project root
    stocks_src = Path(__file__).parent / "src"
    if str(stocks_src) not in sys.path:
        sys.path.insert(0, str(stocks_src))

    # Change cwd so .env is found by pydantic-settings
    os.chdir(Path(__file__).parent)

    from equity_intel.db.models import TickerDiscoveryScore
    from equity_intel.db.session import SessionLocal

    with SessionLocal() as session:
        rows = (
            session.query(TickerDiscoveryScore)
            .filter(
                TickerDiscoveryScore.week_key == week_key,
                TickerDiscoveryScore.recommendation == "probe_candidate",
            )
            .order_by(TickerDiscoveryScore.total_score.desc())
            .all()
        )
        # Detach from session by converting to plain dicts
        return [
            {
                "ticker":                r.ticker,
                "week_key":              r.week_key,
                "total_score":           r.total_score,
                "mention_count":         r.mention_count,
                "prior_week_count":      r.prior_week_count,
                "four_week_avg":         r.four_week_avg,
                "acceleration_score":    r.acceleration_score,
                "mention_volume_score":  r.mention_volume_score,
                "source_quality_score":  r.source_quality_score,
                "breadth_score":         r.breadth_score,
                "novelty_score":         r.novelty_score,
                "unique_source_count":   r.unique_source_count,
                "evidence_json":         r.evidence_json or [],
            }
            for r in rows
        ]


# ── HTML builder ──────────────────────────────────────────────────────────────

def _score_bar(value: float, color: str = "#e53e3e") -> str:
    pct = min(100, round(value * 100))
    return (
        f'<div style="background:#2d1a1a;border-radius:4px;height:6px;width:100%;margin-top:4px;">'
        f'<div style="background:{color};width:{pct}%;height:6px;border-radius:4px;"></div>'
        f'</div>'
    )


def _evidence_html(evidence: list) -> str:
    if not evidence:
        return ""
    items = ""
    for e in evidence[:3]:
        src = e.get("source_type", "").replace("_", " ").title()
        ctx = (e.get("context") or "")[:160]
        sticker = e.get("source_ticker", "")
        items += (
            f'<div style="border-left:3px solid #7f1d1d;padding:6px 10px;'
            f'margin-bottom:6px;background:#1a0a0a;border-radius:0 4px 4px 0;">'
            f'<div style="font-size:10px;color:#f87171;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.5px;">'
            f'{src}{(" · seen in " + sticker) if sticker else ""}</div>'
            f'<div style="font-size:12px;color:#fca5a5;margin-top:2px;">{ctx}</div>'
            f'</div>'
        )
    return items


def _ticker_card(r: dict) -> str:
    score_pct  = round(r["total_score"] * 100)
    accel_pct  = round(r["acceleration_score"] * 100)
    qual_pct   = round(r["source_quality_score"] * 100)
    wow_delta  = r["mention_count"] - r["prior_week_count"]
    wow_str    = f"+{wow_delta}" if wow_delta >= 0 else str(wow_delta)

    return f"""
    <div style="background:#1c0808;border:2px solid #e53e3e;border-radius:12px;
                padding:20px 22px;margin-bottom:20px;">

      <!-- Ticker header -->
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <div style="background:#e53e3e;color:#fff;font-size:22px;font-weight:900;
                    padding:6px 16px;border-radius:8px;letter-spacing:1px;">
          {r["ticker"]}
        </div>
        <div>
          <div style="font-size:26px;font-weight:900;color:#f87171;">
            {score_pct}% score
          </div>
          <div style="font-size:12px;color:#fca5a5;">
            {r["mention_count"]} mentions this week
            &nbsp;·&nbsp; {wow_str} vs last week
            &nbsp;·&nbsp; avg {r["four_week_avg"]:.1f}/wk (4-wk)
          </div>
        </div>
      </div>

      <!-- Score bars -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 20px;margin-bottom:14px;">
        <div>
          <div style="font-size:11px;color:#f87171;font-weight:600;">
            ACCELERATION &nbsp; {accel_pct}%
          </div>
          {_score_bar(r["acceleration_score"], "#e53e3e")}
        </div>
        <div>
          <div style="font-size:11px;color:#f87171;font-weight:600;">
            SOURCE QUALITY &nbsp; {qual_pct}%
          </div>
          {_score_bar(r["source_quality_score"], "#f97316")}
        </div>
        <div>
          <div style="font-size:11px;color:#f87171;font-weight:600;">
            BREADTH &nbsp; {round(r["breadth_score"]*100)}%
            &nbsp;({r["unique_source_count"]} sources)
          </div>
          {_score_bar(r["breadth_score"], "#eab308")}
        </div>
        <div>
          <div style="font-size:11px;color:#f87171;font-weight:600;">
            NOVELTY &nbsp; {round(r["novelty_score"]*100)}%
          </div>
          {_score_bar(r["novelty_score"], "#a78bfa")}
        </div>
      </div>

      <!-- Evidence -->
      {_evidence_html(r["evidence_json"])}

      <!-- CTA -->
      <div style="margin-top:14px;text-align:center;">
        <div style="display:inline-block;background:#e53e3e;color:#fff;
                    font-size:14px;font-weight:900;padding:10px 28px;
                    border-radius:8px;letter-spacing:.5px;text-transform:uppercase;">
          ⚡ ADD {r["ticker"]} TO WATCHLIST
        </div>
      </div>
    </div>"""


def _build_html(candidates: list, week_key: str) -> str:
    n = len(candidates)
    date_str = datetime.now().strftime("%A, %B %d, %Y")
    cards = "".join(_ticker_card(r) for r in candidates)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <style>
    @keyframes flash {{
      0%,100% {{ opacity:1; }}
      50%      {{ opacity:0.3; }}
    }}
    .flash {{ animation: flash 0.9s infinite; display:inline-block; }}
  </style>
</head>
<body style="margin:0;padding:0;background:#0d0101;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:28px 16px;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#7f1d1d 0%,#e53e3e 100%);
                border-radius:16px;padding:32px 28px;margin-bottom:24px;text-align:center;">
      <div style="font-size:48px;margin-bottom:8px;">
        <span class="flash">🚨</span>
        <span class="flash" style="animation-delay:.3s">🚨</span>
        <span class="flash" style="animation-delay:.6s">🚨</span>
      </div>
      <div style="font-size:30px;font-weight:900;color:#fff;
                  text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
        PROBE CANDIDATE ALERT
      </div>
      <div style="font-size:16px;color:rgba(255,255,255,.85);margin-bottom:16px;">
        {date_str} &nbsp;·&nbsp; Week {week_key}
      </div>
      <div style="background:rgba(0,0,0,.35);border-radius:10px;
                  padding:12px 20px;display:inline-block;">
        <span style="font-size:20px;font-weight:900;color:#fff;">
          {n} ticker{"s" if n != 1 else ""} crossed ALL thresholds
        </span>
      </div>
    </div>

    <!-- What this means -->
    <div style="background:#1c0808;border:1px solid #7f1d1d;border-radius:10px;
                padding:14px 18px;margin-bottom:20px;font-size:13px;color:#fca5a5;
                line-height:1.6;">
      <strong style="color:#f87171;">What triggered this alert:</strong>
      each ticker below hit ≥8 mentions, ≥70% composite score, ≥50% acceleration,
      and appeared across ≥3 independent sources — all in the current week.
      These are <strong style="color:#f87171;">not in your watchlist yet.</strong>
    </div>

    <!-- Ticker cards -->
    {cards}

    <!-- Footer -->
    <div style="text-align:center;font-size:12px;color:#7f1d1d;padding-top:8px;">
      Equity Intelligence · {date_str}
    </div>

  </div>
</body>
</html>"""


# ── Send ──────────────────────────────────────────────────────────────────────

def _send(html: str, cfg: dict, week_key: str, count: int) -> None:
    subject = f"🚨 PROBE ALERT — {count} new ticker{'s' if count != 1 else ''} crossed threshold [{week_key}]"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["gmail_address"]
    msg["To"]      = cfg["to_address"]
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(cfg["gmail_address"], cfg["app_password"])
        server.sendmail(cfg["gmail_address"], cfg["to_address"], msg.as_string())


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    week_key = _current_week_key()

    try:
        candidates = _fetch_probe_candidates(week_key)
    except Exception as exc:
        print(f"[probe_alert] DB query failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if not candidates:
        print(f"[probe_alert] No probe candidates for {week_key} — no email sent.")
        return

    print(f"[probe_alert] {len(candidates)} probe candidate(s) for {week_key}: "
          f"{', '.join(r['ticker'] for r in candidates)}")

    try:
        cfg = _load_config()
    except Exception as exc:
        print(f"[probe_alert] Config load failed: {exc}", file=sys.stderr)
        sys.exit(1)

    html = _build_html(candidates, week_key)

    try:
        _send(html, cfg, week_key, len(candidates))
        print(f"[probe_alert] Alert sent to {cfg['to_address']}.")
    except Exception as exc:
        print(f"[probe_alert] Email send failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
