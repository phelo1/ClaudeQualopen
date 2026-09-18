"""Operator settings: strategy parameters and connection credentials, saved
next to the trader state and edited from the dashboard's settings page.

Two files in the state directory:

``settings.yaml``
    the full ``StrategyConfig`` (every section, every parameter). Once it
    exists it is the strategy the session runs - it takes over from
    ``--config`` - so what you see on the settings page is what trades.
``settings.env``
    ``KEY=value`` lines for everything qmag reads from the environment:
    data-source choice, API keys, broker endpoints, LLM keys. Written with
    mode 0600. On load the values are exported into ``os.environ`` (a saved
    value wins over one inherited from the shell) so every module keeps
    reading credentials the same way it always did.

Both files can be exported together as one YAML bundle and imported again -
with or without the secrets - to move a desk between machines or keep a
backup. Secrets are never rendered back to the browser: the page shows a
masked tail (``••••abcd``) and leaving a field blank keeps the saved value.
"""

from __future__ import annotations

import io
import logging
import os
import re
import stat
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_EDGE_WEIGHTS, StrategyConfig
from .data import DATA_KINDS

log = logging.getLogger(__name__)

MASK = "••••"
CLEAR_TOKEN = "__clear__"  # form value meaning "forget the saved value"
BUNDLE_KIND = "qmag-settings"
BUNDLE_VERSION = 1


# --------------------------------------------------------------------------- #
# Everything qmag reads from the environment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EnvField:
    name: str
    label: str
    group: str
    help: str = ""
    secret: bool = False
    kind: str = "text"  # text | password | number | select
    choices: tuple[str, ...] = ()
    placeholder: str = ""


ENV_GROUPS: dict[str, str] = {
    "data": "Price data",
    "uw": "Unusual Whales",
    "alpaca": "Alpaca",
    "ibkr": "Interactive Brokers",
    "mt5": "MetaTrader 5",
    "reddit": "Reddit",
    "alerts": "Alerts (Telegram / webhook)",
    "remote": "Remote access",
    "desks": "Accounts (several desks)",
    "engine": "Engine",
}

ENV_GROUP_HELP: dict[str, str] = {
    "data": "Where daily bars come from. 'auto' uses Interactive Brokers when TWS / IB Gateway is answering on the saved host:port (keeping Unusual Whales for flow and edge), else Unusual Whales when its key is saved, else Yahoo Finance. There is no simulated source.",
    "uw": "Paid API used for price data, the unusual options-flow scan and the 22-feature edge score. Keys: unusualwhales.com → API.",
    "alpaca": "Needed for --broker alpaca-paper / alpaca-live. Paper and live keys are different; use the pair that matches the broker you start.",
    "ibkr": "TWS or IB Gateway must be running with API access enabled. Ports: 7497 paper / 7496 live (TWS), 4002 / 4001 (Gateway). The same connection also serves daily bars: with the data source on 'auto' qmag reads prices from IB whenever the gateway answers (delayed bars unless a market-data subscription is shared with the paper account) and falls back to Yahoo for the symbols IB cannot serve.",
    "mt5": "Windows only. Leave login blank to attach to the terminal that is already logged in.",
    "reddit": "Optional. Without an app the social read uses StockTwits only and Reddit shows NOT CONFIGURED.",
    "remote": "Password for the dashboard when it is reachable from outside this machine (a tunnel, a VPN or a public bind address). With a password set every page and API call needs a sign-in (7-day cookie) or an `Authorization: Bearer <password>` header; five wrong attempts lock the client out for 15 minutes. Leave it empty only when the dashboard is bound to 127.0.0.1.",
    "alerts": "Optional push notifications: fills, exits with their R, kill switch, daily loss limit, data blocking entries, failed cycles / tasks / order tests. Telegram needs a bot token and your chat id (message the bot once, then read the id from getUpdates); the webhook gets JSON with `text` (Slack) and `content` (Discord).",
    "desks": "One desk (state directory + daemon) drives one broker account; orders are never mirrored between accounts. To trade several accounts run several desks (deploy/add-desk.sh) and list the others here: the accounts page then shows every account's equity, cash, holdings and P&L from the snapshot each desk writes after its cycles, and can throw or clear their kill switches.",
    "engine": "Miscellaneous engine switches.",
}

ENV_FIELDS: tuple[EnvField, ...] = (
    EnvField("QMAG_DATA", "Data source", "data", kind="select", choices=DATA_KINDS,
             help="Applies when the command line leaves --data at 'auto'. Choosing a source here is what the daemon and dashboard use after the next reload."),
    EnvField("UNUSUAL_WHALES_API_KEY", "API key", "uw", secret=True, help="Bearer token from the Unusual Whales dashboard."),
    EnvField("UNUSUAL_WHALES_RPM", "Requests per minute", "uw", kind="number", placeholder="100", help="Process-wide throttle. Match your plan's rate limit."),
    EnvField("UNUSUAL_WHALES_DAILY_CAP", "Daily call cap", "uw", kind="number", placeholder="0",
             help="Stop calling Unusual Whales after this many requests in a UTC day (0 = no cap). Independently, a 'daily limit' answer from the API pauses calls until midnight UTC. Either way the missing readings are recorded as data gaps, never substituted."),
    EnvField("UNUSUAL_WHALES_BULK_THRESHOLD", "Bulk threshold (symbols)", "uw", kind="number", placeholder="300",
             help="Whole-market sweeps above this many symbols are served by Yahoo batches instead of one Unusual Whales call per symbol. 0 = always Unusual Whales."),
    EnvField("ALPACA_API_KEY", "API key ID", "alpaca", secret=True),
    EnvField("ALPACA_SECRET_KEY", "Secret key", "alpaca", secret=True),
    EnvField("IBKR_ACCOUNT", "Trading account", "ibkr", placeholder="DU...", help="Required when the gateway exposes several accounts. All orders and risk calculations use only this account."),
    EnvField("IBKR_HOST", "Host", "ibkr", placeholder="127.0.0.1"),
    EnvField("IBKR_PORT", "Port", "ibkr", kind="number", placeholder="7497"),
    EnvField("IBKR_CLIENT_ID", "Client id (trading)", "ibkr", kind="number", placeholder="17"),
    EnvField("IBKR_DATA_CLIENT_ID", "Client id (data)", "ibkr", kind="number", placeholder="18"),
    EnvField("IBKR_PREFER_DATA", "Use IB for price data when the gateway is up", "ibkr", kind="select", choices=("yes", "no"), placeholder="yes",
             help="Only matters while the data source is 'auto'. 'yes': read daily bars from IB whenever TWS / IB Gateway answers, keeping Unusual Whales for flow and edge. 'no': never pick IB automatically (you can still start with --data ibkr)."),
    EnvField("IBKR_DATA_FALLBACK", "Fill gaps from Yahoo", "ibkr", kind="select", choices=("yes", "no"), placeholder="yes",
             help="Symbols IB has no bars for (no security definition, no permission) are loaded from Yahoo and counted in the report's data stats. 'no' leaves them out instead. Nothing is ever substituted or generated."),
    EnvField("IBKR_VOLUME_MULTIPLIER", "Volume multiplier", "ibkr", placeholder="auto",
             help="IB reports US stock volume in lots of 100 on some gateway versions and in shares on others. Leave on auto: qmag measures the scale once against Yahoo's SPY volume and remembers it for a week. Set 1 or 100 to force it."),
    EnvField("IBKR_DATA_CONCURRENCY", "Historical requests in flight", "ibkr", kind="number", placeholder="8",
             help="How many daily-bar requests run at once. 8 keeps a whole-market cold load to minutes while staying under IB's pacing limits; lower it if the gateway logs pacing violations."),
    EnvField("MT5_PATH", "Terminal path", "mt5", placeholder=r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    EnvField("MT5_LOGIN", "Login", "mt5", kind="number"),
    EnvField("MT5_PASSWORD", "Password", "mt5", secret=True),
    EnvField("MT5_SERVER", "Server", "mt5"),
    EnvField("MT5_SYMBOL_PREFIX", "Symbol prefix", "mt5"),
    EnvField("MT5_SYMBOL_SUFFIX", "Symbol suffix", "mt5", placeholder=".US"),
    EnvField("MT5_MAGIC", "Magic number", "mt5", kind="number", placeholder="260901"),
    EnvField("REDDIT_CLIENT_ID", "Client id", "reddit"),
    EnvField("REDDIT_CLIENT_SECRET", "Client secret", "reddit", secret=True),
    EnvField("REDDIT_USER_AGENT", "User agent", "reddit", placeholder="qmag/0.1 by <your reddit name>"),
    EnvField("TELEGRAM_BOT_TOKEN", "Telegram bot token", "alerts", secret=True, help="From @BotFather. Never shown again once saved."),
    EnvField("TELEGRAM_CHAT_ID", "Telegram chat id", "alerts", placeholder="123456789", help="Your user or group id; the bot must have received a message from that chat first."),
    EnvField("RESEND_API_KEY", "Resend API key", "alerts", secret=True, help="Email delivery key; retained when migrating an existing email alert configuration."),
    EnvField("QMAG_ALERT_EMAIL_FROM", "Alert email sender", "alerts", help="Sender on a domain verified in your Resend account."),
    EnvField("QMAG_ALERT_EMAIL_TO", "Alert email recipients", "alerts", help="Comma-separated recipients for warnings and errors."),
    EnvField("QMAG_ALERT_EMAIL_TRADES", "Email routine trading updates", "alerts", kind="select", choices=("no", "yes"), placeholder="no", help="Warnings and errors are emailed by default. Enable to include routine information and fills."),
    EnvField("QMAG_ALERT_WEBHOOK_URL", "Webhook URL", "alerts", secret=True, placeholder="https://hooks.slack.com/services/…",
             help="Slack / Discord incoming webhook or any endpoint accepting JSON POSTs. Treated as a secret because these URLs grant posting rights."),
    EnvField("QMAG_DASHBOARD_PASSWORD", "Dashboard password", "remote", secret=True,
             help="At least 8 characters. Saving a new one signs every other browser out immediately; clearing it removes the sign-in page (only safe when the dashboard is bound to 127.0.0.1)."),
    EnvField("QMAG_DESK_NAME", "This desk's name", "desks", placeholder="ibkr-paper",
             help="Label for this account on the accounts page and in alerts. Defaults to the state directory's name."),
    EnvField("QMAG_DESKS", "Other desks to show", "desks", placeholder="alpaca=/home/ubuntu/qmag/desks/alpaca|http://127.0.0.1:8856",
             help="Comma-separated `name=/path/to/state_dir` entries, optionally `|http://host:port` for that desk's own dashboard. Their account.json, daemon heartbeat and halt file are read as-is - nothing is computed for a desk that has not written a snapshot."),
    EnvField("QMAG_SCORER", "Headline sentiment scorer", "engine", kind="select", choices=("vader", "finbert"),
             help="finbert needs `pip install qmag[finbert]` (transformers + torch)."),
)
ENV_BY_NAME = {f.name: f for f in ENV_FIELDS}
# Model keys live in llm_providers.json (settings → AI providers); these
# environment variables remain honoured as fallbacks and are redacted too.
LLM_ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "QMAG_LLM_API_KEY")
SECRET_NAMES = frozenset([f.name for f in ENV_FIELDS if f.secret] + list(LLM_ENV_KEYS))


def mask(value: str | None) -> str:
    """Never echo a secret back: a fixed mask plus the last four characters when long enough."""
    if not value:
        return ""
    return MASK + value[-4:] if len(value) >= 10 else MASK


# --------------------------------------------------------------------------- #
# .env file
# --------------------------------------------------------------------------- #
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out[key] = raw
    return out


def dump_env(values: dict[str, str]) -> str:
    known = [f.name for f in ENV_FIELDS]
    order = sorted(values, key=lambda k: (known.index(k) if k in known else len(known), k))
    lines = ["# qmag connection settings - written by the settings page. Keep this file private.", ""]
    for key in order:
        v = values[key]
        if v == "" or v is None:
            continue
        needs_quotes = any(ch in v for ch in " #\"'\\") or v != v.strip()
        lines.append(f"{key}={_quote(v) if needs_quotes else v}")
    return "\n".join(lines) + "\n"


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_private(path: Path, text: str) -> None:
    """Write with mode 0600 (owner read/write only), creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platforms without POSIX modes
        pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class SettingsStore:
    """The settings files of one state directory."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.yaml_path = self.state_dir / "settings.yaml"
        self.env_path = self.state_dir / "settings.env"
        self.providers_path = self.state_dir / "llm_providers.json"
        self._applied: dict[str, str] = {}

    # ---- change detection ------------------------------------------------
    def signature(self) -> tuple:
        def sig(p: Path):
            try:
                st = p.stat()
                return (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                return None

        return (sig(self.yaml_path), sig(self.env_path), sig(self.providers_path))

    # ---- strategy ----------------------------------------------------------
    def load_config(self) -> StrategyConfig | None:
        return StrategyConfig.load(self.yaml_path) if self.yaml_path.exists() else None

    def save_config(self, cfg: StrategyConfig) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.save(self.yaml_path)
        return self.yaml_path

    def reset_config(self) -> None:
        if self.yaml_path.exists():
            self.yaml_path.unlink()

    # ---- environment -----------------------------------------------------
    def load_env(self) -> dict[str, str]:
        if not self.env_path.exists():
            return {}
        try:
            return parse_env(self.env_path.read_text())
        except OSError as exc:  # pragma: no cover
            log.warning("could not read %s: %s", self.env_path, exc)
            return {}

    def save_env(self, values: dict[str, str]) -> Path:
        clean = {k: str(v) for k, v in values.items() if v not in (None, "")}
        write_private(self.env_path, dump_env(clean))
        return self.env_path

    def apply_env(self) -> dict[str, str]:
        """Export the saved values into ``os.environ``.

        A saved value wins over the inherited shell value. A key that was
        saved earlier in this process and has since been removed from the
        file is unset again, so clearing a key on the settings page takes
        effect without a restart (unless the shell itself provides it, in
        which case the inherited value shows as source ``environment``).
        """
        saved = self.load_env()
        for key, old in self._applied.items():
            if key not in saved and os.environ.get(key) == old:
                os.environ.pop(key, None)
        for key, value in saved.items():
            os.environ[key] = value
        self._applied = dict(saved)
        return saved

    def env_view(self) -> list[dict[str, Any]]:
        """Per-field state for the page: value (masked for secrets), where it came from."""
        saved = self.load_env()
        rows = []
        for f in ENV_FIELDS:
            in_file = f.name in saved and saved[f.name] != ""
            env_val = os.environ.get(f.name)
            value = saved[f.name] if in_file else (env_val or "")
            source = "saved" if in_file else "environment" if env_val else "unset"
            rows.append({
                "name": f.name, "label": f.label, "group": f.group, "help": f.help, "secret": f.secret, "kind": f.kind,
                "choices": list(f.choices), "placeholder": f.placeholder, "set": bool(value),
                "display": mask(value) if f.secret else value, "source": source,
            })
        return rows

    def env_groups(self) -> list[dict[str, Any]]:
        rows = self.env_view()
        return [
            {"key": g, "label": label, "help": ENV_GROUP_HELP.get(g, ""), "fields": [r for r in rows if r["group"] == g],
             "configured": sum(1 for r in rows if r["group"] == g and r["set"])}
            for g, label in ENV_GROUPS.items()
        ]

    def update_env_from_form(self, form: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        """Merge a submitted connections form into the saved values.

        Fields absent from the form are untouched. Secrets: blank keeps the
        saved value, ``CLEAR_TOKEN`` (the "forget" checkbox) removes it,
        anything else replaces it. Plain fields are taken literally (blank =
        not saved). Returns (values, changed keys).
        """
        saved = self.load_env()
        new = dict(saved)
        changed: list[str] = []
        for f in ENV_FIELDS:
            if f.name not in form and f"{f.name}__clear" not in form:
                continue
            raw = (form.get(f.name) or "").strip()
            clear = form.get(f"{f.name}__clear") in ("1", "on", "true", CLEAR_TOKEN) or raw == CLEAR_TOKEN
            if clear:
                if f.name in new:
                    new.pop(f.name)
                    changed.append(f.name)
                continue
            if f.secret:
                if raw == "" or raw.startswith(MASK):
                    continue  # keep what is saved
                if new.get(f.name) != raw:
                    new[f.name] = raw
                    changed.append(f.name)
                continue
            if f.kind == "number" and raw:
                try:
                    float(raw)
                except ValueError:
                    raise ValueError(f"{f.label}: '{raw}' is not a number")
            if f.kind == "select" and raw and raw not in f.choices:
                raise ValueError(f"{f.label}: '{raw}' is not one of {', '.join(f.choices)}")
            if raw == "":
                if f.name in new:
                    new.pop(f.name)
                    changed.append(f.name)
            elif new.get(f.name) != raw:
                new[f.name] = raw
                changed.append(f.name)
        return new, changed

    # ---- bundle export / import ------------------------------------------
    def export_bundle(self, cfg: StrategyConfig, include_secrets: bool) -> str:
        from .providers import ProviderStore

        env = self.load_env()
        connections = {k: v for k, v in env.items() if include_secrets or k not in SECRET_NAMES}
        omitted = sorted(k for k in env if k in SECRET_NAMES and not include_secrets)
        pstore = ProviderStore(self.state_dir)
        if not include_secrets:
            omitted += [f"ai provider key: {r['id']}" for r in pstore.load_rows() if r.get("api_key")]
        bundle = {
            "kind": BUNDLE_KIND,
            "version": BUNDLE_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "includes_secrets": include_secrets,
            "secrets_omitted": omitted,
            "strategy": cfg.to_dict(),
            "connections": connections,
            "ai_providers": pstore.export_rows(include_secrets),
        }
        buf = io.StringIO()
        buf.write("# qmag settings bundle. Import it on the dashboard's Settings page.\n")
        if include_secrets:
            buf.write("# CONTAINS API KEYS - treat this file like a password.\n")
        yaml.safe_dump(bundle, buf, sort_keys=False)
        return buf.getvalue()

    @staticmethod
    def parse_bundle(text: str) -> tuple[StrategyConfig | None, dict[str, str], list[str]]:
        cfg, conn, notes, _providers = SettingsStore.parse_bundle_full(text)
        return cfg, conn, notes

    @staticmethod
    def parse_bundle_full(text: str) -> tuple[StrategyConfig | None, dict[str, str], list[str], list[dict[str, Any]]]:
        """Validate an uploaded bundle (or a bare strategy YAML).

        Returns (strategy or None, connection values, notes). Raises
        ``ValueError`` with a readable message when the file is unusable.
        """
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("the file must contain a YAML mapping")
        notes: list[str] = []
        providers_raw: list[dict[str, Any]] = []
        if raw.get("kind") == BUNDLE_KIND or "strategy" in raw or "connections" in raw:
            strategy_raw = raw.get("strategy")
            conn_raw = raw.get("connections") or {}
            pr = raw.get("ai_providers") or []
            if not isinstance(pr, list) or not all(isinstance(r, dict) for r in pr):
                raise ValueError("'ai_providers' must be a list of provider rows")
            providers_raw = pr
        else:  # a plain strategy YAML (the same format `--config` reads)
            strategy_raw, conn_raw = raw, {}
            notes.append("plain strategy YAML: no connection settings in this file")
        cfg = None
        if strategy_raw is not None:
            if not isinstance(strategy_raw, dict):
                raise ValueError("'strategy' must be a mapping of sections")
            try:
                cfg = StrategyConfig.from_dict(strategy_raw)
            except TypeError as exc:
                raise ValueError(f"strategy section is malformed: {exc}") from exc
            validate_config(cfg)
        if not isinstance(conn_raw, dict):
            raise ValueError("'connections' must be a mapping of KEY: value")
        conn: dict[str, str] = {}
        unknown: list[str] = []
        for k, v in conn_raw.items():
            key = str(k)
            if key not in ENV_BY_NAME:
                unknown.append(key)
                continue
            if v is None or str(v) == "":
                continue
            conn[key] = str(v)
        if unknown:
            notes.append("ignored unknown connection keys: " + ", ".join(sorted(unknown)))
        return cfg, conn, notes, providers_raw

    def import_bundle(self, text: str, include_secrets: bool = True) -> dict[str, Any]:
        from .providers import ProviderStore

        cfg, conn, notes, providers_raw = self.parse_bundle_full(text)
        if cfg is not None:
            self.save_config(cfg)
        provider_ids = ProviderStore(self.state_dir).import_rows(providers_raw, include_secrets=include_secrets) if providers_raw else []
        applied_keys: list[str] = []
        if conn:
            current = self.load_env()
            for k, v in conn.items():
                if k in SECRET_NAMES and not include_secrets:
                    continue
                current[k] = v
                applied_keys.append(k)
            self.save_env(current)
        return {"strategy": cfg is not None, "connections": sorted(applied_keys), "ai_providers": provider_ids, "notes": notes}


# --------------------------------------------------------------------------- #
# Strategy config <-> HTML form
# --------------------------------------------------------------------------- #
SECTION_LABELS: dict[str, str] = {
    "autonomy": "Autonomous research & improvement",
    "momentum": "Momentum filter",
    "breakout": "Breakout setup",
    "episodic_pivot": "Episodic pivot setup",
    "management": "Trade management (exits)",
    "risk": "Risk & sizing",
    "regime": "Market regime filter",
    "themes": "Theme / sector momentum",
    "sentiment": "Sentiment CSV filter",
    "context": "Live context (news, social, events)",
    "options_flow": "Unusual options flow",
    "edge": "Unusual Whales edge score",
    "adaptive": "Adaptive risk",
    "reviewer": "LLM trade reviewer",
    "committee": "Analyst committee (bull / bear / risk chair)",
    "schedule": "Scan schedule (tiered)",
    "insider_scan": "Saturday insider / unusual-options scan",
    "entry": "Entry trigger (confirmed / resting / hybrid)",
    "learning": "Learning from trades (journal, shadows, auto-tuning)",
    "advisor": "Desk advisor (plain-English settings manager)",
}

SECTION_DOCS: dict[str, str] = {
    "risk": "How much is risked per trade and in total. Shares = risk dollars / (entry − stop), capped by position and exposure limits.",
}

# Sections whose first sentence is not enough on the settings page.
SECTION_SUMMARY: dict[str, str] = {
    "schedule": "One whole-universe scan a night builds the arming list; pre-market gap scan, 5-minute focused passes on the arming list and a movers sweep run during the session.",
    "insider_scan": "Every Saturday the week's most unusual options trades are flagged for investigation and an AI is asked what catalyst they could be tied to and what the buyer may be speculating on.",
    "entry": "confirmed: buy only when the live bar takes out and holds the pivot on paced volume, outside the opening range (no resting orders). resting: park stop-limit brackets overnight. hybrid: park them after the open. Fresh positions that fall back below the pivot are sold as failed breakouts.",
    "learning": "Every trade is journaled with the features it was taken on and a post-mortem; rejected setups are followed as shadow trades on real bars. The weekly review writes lessons and moves a bounded set of selection / trigger knobs one step at a time, with the evidence, into learning_overrides.yaml.",
}

# Plain-language help for the parameters operators touch most.
FIELD_HELP: dict[str, str] = {
    "autonomy.enabled": 'Run scheduled candidate research and prospective paper comparisons automatically. Does not enable live brokerage access.',
    "autonomy.auto_promote": 'Automatically promote a candidate after prospective evidence passes the published gates; automatically roll back on deterioration.',
    "autonomy.research_weekday": 'Weekday for background candidate discovery.',
    "autonomy.research_time": 'New York time for weekly background candidate discovery.',
    "autonomy.min_history_days": 'Minimum completed trading sessions for historical candidate research, including warm-up.',
    "autonomy.min_forward_days": 'Minimum newly observed trading sessions before promotion. Never reuse the discovery period.',
    "autonomy.min_forward_trades": 'Minimum closed candidate paper trades before prospective promotion.',
    "autonomy.max_trial_days": 'Expire an inconclusive trial after this number of calendar days.',
    "autonomy.max_candidates": 'Maximum one-step candidates tried in a research batch; all trials are recorded.',
    "autonomy.minimum_return_lift": 'Minimum paired prospective portfolio return improvement over the frozen baseline.',
    "autonomy.max_drawdown": 'Maximum forward candidate drawdown allowed for promotion, expressed as a fraction.',
    "autonomy.rollback_drawdown": 'Roll back a promoted candidate when its forward portfolio drawdown exceeds this fraction.',
    "autonomy.canary_risk_fraction": 'Fraction of the operator risk budget used for a newly promoted live candidate. Always at most one.',
    "autonomy.canary_trades": 'Minimum broker-verified closed canary trades before returning to the operator risk budget.',
    "autonomy.train_outcome_model": 'Fit an interpretable model from entry features and outcomes; validate chronologically and forward-test before using its weights.',
    "autonomy.min_model_records": 'Minimum outcome records for fitting. Shadow samples have lower training weight and cannot be validation outcomes.',
    "autonomy.retention_days": 'Retain generated chart/report artifacts for at least this many days. Never prune execution or trade records.',

    "risk.starting_equity": "Only used by the built-in paper ledger when it is created.",
    "risk.risk_per_trade_pct": "Fraction of equity lost if the initial stop is hit. 0.005 = 0.5 %.",
    "risk.max_position_pct": "Cap on one position as a fraction of equity.",
    "risk.max_positions": "Open positions plus pending entries never exceed this.",
    "risk.max_gross_exposure": "1.0 = fully invested, no margin.",
    "risk.max_portfolio_heat_pct": "Cap on open heat: what every position and resting entry would lose at its stop, plus the new trade, as a fraction of equity. 0 = off.",
    "risk.daily_loss_limit_pct": "Circuit-breaker: once equity is down this much since the first pass of the session, no new entries until tomorrow (exits still managed). 0 = off.",
    "risk.max_positions_per_theme": "At most this many open or pending positions in one theme. 0 = off.",
    "management.stop_adr_mult": "Initial stop = entry − this × ADR (1.0 approximates a low-of-day stop).",
    "management.max_stop_pct": "Setups whose stop is wider than this fraction of price are skipped.",
    "management.partial_after_days": "Sell the partial at the close of this day if the trade is green (whichever comes first with the R target).",
    "management.partial_fraction": "Share of the position sold into strength.",
    "management.partial_target_r": "Resting limit at this many R. Blank = time rule only.",
    "management.trail_ma": "Moving average that trails the remainder (10 fast movers, 20 slower).",
    "management.time_stop_days": "Cut a trade that closes at or under its entry after this many sessions without ever reaching the MFE below and before any partial (0 = off). Dead money is risk and slot cost.",
    "management.time_stop_min_mfe_r": "Open profit (in R) the trade must have shown at some point to be spared the time stop.",
    "edge.threshold": "Weighted Unusual Whales edge score (−1..+1) a plan must reach.",
    "edge.min_coverage": "Share of enabled feature weight that must have answered; missing data never passes.",
    "edge.gate": "On: fail the plan below the threshold. Off: record the score, rank on it, never block.",
    "edge.weights": "Weight of each feature inside the score. 0 switches a feature off (not fetched).",
    "edge.max_symbols_per_cycle": "Paid calls: roughly 20 requests per candidate.",
    "options_flow.enabled": "Scan each candidate's options tape for unusual trades (paid Unusual Whales calls).",
    "options_flow.require_bullish": "Only trade with bullish unusual flow present. Missing data fails.",
    "options_flow.min_score": "Veto below this flow tilt (e.g. −0.3). Blank = record only.",
    "reviewer.enabled": "Send the whole edge bundle to an LLM and record its strict-JSON verdict.",
    "reviewer.mode": "advisory records only; gate needs BUY ≥ min_confidence; gate_and_size also scales shares.",
    "reviewer.fail_closed": "An unreachable model skips the trade instead of passing the gate.",
    "regime.enabled": "Only open new positions when the benchmark / breadth / VIX gates pass.",
    "regime.max_vix": "Stand aside above this VIX level. Blank = off.",
    "themes.groups": "Inline theme → tickers mapping (YAML). Blank = use the themes file / industries.",
    "themes.apply_to": "Setups gated on theme strength (comma separated: breakout, episodic_pivot).",
    "context.enabled": "Gather news, social, events and options context for candidates that pass the price filters.",
    "context.social_sources": "Comma separated: stocktwits, reddit.",
    "context.max_symbols_per_cycle": "Cap on outbound context requests per cycle (top-ranked candidates first).",
    "schedule.tiered": "On: nightly arming scan + pre-market gap scan + 5-minute focused passes + movers sweep. Off: legacy 30-minute whole-universe cycles.",
    "schedule.arming_distance_pct": "Flags whose pivot is within this fraction of the last close are armed for the next session (0.05 = 5 %).",
    "schedule.arming_max_names": "Cap on the arming list, best scores first.",
    "schedule.premarket_times": "Comma separated NY times for the pre-market gap scan (needs Unusual Whales or finviz).",
    "schedule.premarket_min_gap_pct": "Pre-market change vs the previous close that makes a gapper (0.08 = 8 %).",
    "schedule.post_open_full_scan": "One whole-universe pass on the first bars of the day so EPs are caught even without a screener.",
    "schedule.focused_interval_minutes": "How often the arming list, open positions and screener hits are refreshed during the session.",
    "schedule.trigger_distance_pct": "Names this close to their pivot get live context / paid edge reads and re-sized plans each pass.",
    "schedule.volume_pace": "Judge a partial bar's volume on projected full-day pace, not the raw count so far.",
    "schedule.movers_min_change_pct": "Day change that puts a name on the movers sweep (0.08 = 8 %).",
    "schedule.movers_min_rvol": "Relative volume (vs 30-day average) a mover needs.",
    "insider_scan.enabled": "Run the weekly unusual-options review on the configured weekday.",
    "insider_scan.min_premium": "Only alerts with at least this much total premium are read (server-side filter).",
    "insider_scan.min_volume_oi_ratio": "Contract volume vs open interest: fresh positioning, not existing holders.",
    "insider_scan.min_otm_pct": "Strike at least this far out of the money (percent).",
    "insider_scan.max_market_cap": "Drop tickers above this market cap (blank = no cap), checked against the API's stock info for each candidate. Mega-cap flow is routine institutional business.",
    "insider_scan.min_flag_score": "0-10 score a ticker needs to be flagged. The score rewards the day-before profile: OTM bet 10-35 % out, 3-45 days to expiry, sweeps / at-the-ask / opening, one strike hit repeatedly, fresh open interest, chain volume many times its 30-day average, expiring before earnings; it penalises crowded mega-cap chains.",
    "insider_scan.max_flagged": "Most tickers sent to the AI per week (one model call each).",
    "insider_scan.ai_enabled": "Ask an LLM (Gemini or OpenAI-compatible) for the possible catalyst and what the buyer may be speculating on.",
    "insider_scan.max_pages": "Flow-alert pages of 200 read per week (paid calls).",
    "entry.mode": "confirmed = live hold + volume pace gate, no resting orders; resting = overnight stop-limit brackets; hybrid = brackets parked after resting_from.",
    "entry.confirm_volume_ratio": "Intraday: volume pacing to at least this × the 20-day average before a confirmed buy (the nightly scan on complete bars still uses breakout.min_breakout_volume_ratio).",
    "entry.require_hold_above_pivot": "The last price must still be above the pivot when the pass runs - a wick through it does not count.",
    "entry.opening_range_minutes": "No confirmed entries this many minutes after 09:30 (opening auction, stop-runs).",
    "entry.resting_from": "hybrid: NY time from which buy-stop brackets are parked for the day.",
    "entry.resting_min_score": "resting / hybrid: only plans scoring at least this park an order.",
    "entry.failed_breakout_exit": "Sell a fresh position at market once price is back below the pivot (completed bar, or intraday on weak paced volume).",
    "entry.failed_breakout_days": "Bars after entry during which the failed-breakout rule applies.",
    "learning.enabled": "Keep the enriched journal, shadow ledger and weekly review.",
    "learning.auto_apply": "Opt in to at most one tightening per review, using 30+ fresh verified executions and a negative upper confidence bound. Shadows cannot apply changes. Default off: proposals only.",
    "learning.min_trades": "Trades (or shadow trades) a bucket needs before it can teach anything.",
    "learning.min_lift_r": "Bucket expectancy must differ from the overall by at least this many R to count as a lesson / adjustment.",
    "learning.shadow_enabled": "Follow rejected and untaken setups on real bars to judge the filters.",
    "learning.shadow_max_days": "Sessions a shadow setup has to trigger before it expires.",
    "learning.llm_enabled": "Add a strict-JSON post-mortem per closed trade from the configured model (optional).",
    # -- momentum filter
    "momentum.min_gain_1m": "A stock qualifies as a leader if it is up at least this fraction over ~21 trading days (0.30 = 30 %) - or passes the 3-month or 6-month bar.",
    "momentum.min_gain_3m": "... or up this fraction over ~63 trading days (0.50 = 50 %).",
    "momentum.min_gain_6m": "... or up this fraction over ~126 trading days (1.00 = 100 %).",
    "momentum.min_adr_pct": "Average daily range in percent (20-day). Names that do not move cannot pay for their risk; he prefers > 5.",
    "momentum.min_price": "Minimum share price. Below a few dollars the tape is erratic and fills are poor.",
    "momentum.min_dollar_volume": "Minimum 20-day average close × volume in dollars, so the position can be entered and exited without moving the stock.",
    # -- breakout setup
    "breakout.enabled": "Detect momentum breakouts out of tight flags.",
    "breakout.min_flag_days": "Shortest consolidation (in bars) after the impulse that counts as a flag.",
    "breakout.max_flag_days": "Longest consolidation that still counts as a flag; older bases are a different setup.",
    "breakout.max_flag_depth": "How much of the prior impulse the flag may give back (0.5 = half). Deeper pullbacks are disorderly and skipped.",
    "breakout.max_recent_adr_ratio": "Range contraction: ADR over the last 5 days divided by ADR over the whole flag must be at most this. Tightness is the tell.",
    "breakout.require_above_ma": "Price must be riding above its rising short moving averages (surfing the 10/20).",
    "breakout.fast_ma": "Fast moving average (bars) used for the surf test and the trigger context.",
    "breakout.slow_ma": "Slow moving average (bars) used for the surf test.",
    "breakout.min_breakout_volume_ratio": "Trigger-day volume vs the 20-day average on a completed bar (1.2 = 20 % above average).",
    "breakout.max_gap_pct": "Skip a breakout that opens more than this fraction above the pivot - chasing a gap has a poor stop.",
    "breakout.entry_buffer_pct": "Entry sits this fraction above the pivot (0.002 = 0.2 %), approximating an opening-range-high buy.",
    # -- episodic pivot
    "episodic_pivot.enabled": "Detect episodic pivots: gap-ups on huge volume from a neglected base (earnings-style).",
    "episodic_pivot.min_gap_pct": "Gap over the previous close that qualifies (0.10 = 10 %).",
    "episodic_pivot.min_volume_ratio": "Volume vs the 50-day average on the gap day (3.0 = 3× normal).",
    "episodic_pivot.max_prior_gain_3m": "The stock must NOT already be extended: skip if it is up more than this over 3 months.",
    "episodic_pivot.min_close_position": "Where the close sits in the day's range (0.6 = upper 40 %): the gap must hold.",
    "episodic_pivot.entry_buffer_pct": "Entry sits this fraction above the opening-range high proxy.",
    # -- management / risk extras
    "management.move_stop_to_breakeven": "After the partial, lift the stop to the entry price so the rest cannot become a loser.",
    "management.max_hold_days": "Hard maximum hold in sessions; whatever is left is sold at the close.",
    "risk.commission_per_share": "Per-share commission used by the backtester and the paper ledger.",
    "risk.slippage_bps": "Slippage in basis points applied to simulated entries and exits (5 = 0.05 %).",
    # -- regime
    "regime.benchmark": "Index ETF whose trend gates new longs (QQQ for growth momentum).",
    "regime.ma_length": "Moving average (bars) the benchmark must be above.",
    "regime.breadth_enabled": "Also require a share of the scan universe to be above its moving average - a direct read of whether momentum names are working.",
    "regime.breadth_ma_length": "Moving average (bars) used for the breadth count.",
    "regime.min_breadth": "Share of the universe above its MA needed (0.40 = 40 %).",
    "regime.vix_symbol": "Volatility index symbol used by max_vix (must be available from the data source).",
    # -- themes
    "themes.min_theme_members": "A theme needs at least this many scanned members with bars to rank; smaller groups are ignored (their stocks count as themeless).",
    "themes.enabled": "Rank every signal by the momentum of the themes / industries it belongs to and gate breakouts on it.",
    "themes.themes_file": "Hand-kept theme → tickers YAML. Blank = universe/themes.yaml.",
    "themes.use_industries": "Add one automatic theme per finviz industry from the cached fundamentals, so the whole market is covered.",
    "themes.min_industry_members": "Industries with fewer scanned members than this do not form a theme.",
    "themes.min_theme_percentile": "Gate: skip breakouts whose best theme ranks below this percentile of all themes (0.3 = bottom 30 % skipped).",
    "themes.min_theme_breadth": "Gate: at least this share of the theme's members must be above their 20-day MA.",
    "themes.require_theme": "On: ignore stocks that belong to no theme at all.",
    "themes.score_weight": "How much the theme percentile adds to a setup's ranking score (soft: decides who gets the slot).",
    # -- sentiment CSV
    "sentiment.enabled": "Use a per-symbol, per-day historical sentiment file (bring your own data) in ranking and gating.",
    "sentiment.source": "Sentiment provider; the bundled one is csv.",
    "sentiment.path": "Path to a date,symbol,score[,buzz] CSV.",
    "sentiment.min_score": "Skip a setup when its sentiment is below this (−1..+1). Blank = no floor.",
    "sentiment.max_score": "Skip when sentiment is above this - a crowded-trade guard. Blank = no ceiling.",
    "sentiment.score_weight": "Contribution of sentiment to the ranking score.",
    "sentiment.max_staleness_days": "A reading older than this many days is treated as missing.",
    # -- live context
    "context.news_enabled": "Pull recent headlines (finviz, Yahoo fallback) and score their tone and catalyst tags.",
    "context.news_lookback_days": "Only headlines newer than this are scored.",
    "context.news_min_score": "Veto breakouts when the news tone is below this (−1..+1). Blank = never veto.",
    "context.social_enabled": "Read StockTwits (and Reddit when configured) for chatter volume and tone.",
    "context.social_min_score": "Veto when social tone is below this. Blank = off.",
    "context.social_max_score": "Crowded-trade guard: veto when social tone is above this (near-unanimous bullishness). Blank = off.",
    "context.social_min_messages": "Fewer messages than this reads as quiet, not as a signal.",
    "context.events_enabled": "Look up the next earnings date, float and short interest.",
    "context.avoid_earnings_within_days": "Breakouts only: do not open a fresh breakout with earnings this close (a print is a coin flip).",
    "context.small_float_shares": "Float below this many shares is flagged low float - bigger moves both ways.",
    "context.high_short_float_pct": "Short interest above this percent of float is flagged as squeeze fuel.",
    "context.score_weight": "Weight of the composite context score (−1..+1) inside the ranking score.",
    "context.cache_minutes": "How long a symbol's context is reused before it is fetched again.",
    "committee.enabled": "Convene the three-seat analyst committee on every plan the rules like: a bull researcher, a bear researcher and a risk chair, each a separate model call.",
    "committee.can_veto": "On: the chair's reject blocks the trade and a reduce scales the shares. Off: the ruling is recorded and shown, the checklist decides.",
    "committee.debate": "On: the bear reads the bull case before writing and rebuts it; the chair reads both. Off: bull and bear write blind.",
    "committee.timeout_seconds": "Seconds to wait for each seat's answer.",
    "committee.max_plans_per_cycle": "Most plans the committee sits on per cycle (three model calls each); later plans are noted as not convened.",
    "committee.bull_provider": "Which AI provider answers for the bull researcher (settings → AI providers). auto = the first provider with a key.",
    "committee.bull_model": "Model for the bull researcher. Blank = the provider's default where it has one; the list shows what your key can call.",
    "committee.bull_base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    "committee.bear_provider": "Which AI provider answers for the bear researcher - pick a different vendor from the bull for a genuine second opinion.",
    "committee.bear_model": "Model for the bear researcher. Blank = the provider's default where it has one.",
    "committee.bear_base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    "committee.risk_provider": "Which AI provider answers for the risk chair, who reads both cases and rules take / reduce / reject.",
    "committee.risk_model": "Model for the risk chair. Blank = the provider's default where it has one; give the chair the strongest model you have.",
    "committee.risk_base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    # -- options flow
    "options_flow.provider": "Options-flow data provider (unusual_whales).",
    "options_flow.lookback_days": "Flow alerts newer than this many days are read.",
    "options_flow.min_premium": "Alerts below this total premium (dollars) are ignored.",
    "options_flow.unusual_preset": "Ask for Unusual Whales' own 'unusual' preset: volume > OI, opening, OTM, single-leg, ask-side, ≥ $10k.",
    "options_flow.max_dte": "Ignore contracts expiring further out than this many days.",
    "options_flow.sweep_weight": "Intermarket sweeps are urgent buying; weigh them this much more than ordinary prints.",
    "options_flow.top_trades": "How many of the largest unusual trades are kept for the plan page and the reviewer.",
    "options_flow.weight": "Weight of the flow tilt inside the composite context score.",
    "options_flow.volume_ratio_unusual": "Call volume at least this × its 30-day average counts as unusual.",
    "options_flow.percentile_unusual": "Option-volume percentile vs the ticker's own history that counts as unusual.",
    "options_flow.bullish_threshold": "Flow tilt (−1..+1) at or above which the flow counts as bullish for require_bullish.",
    "options_flow.min_alerts": "Unusual trades needed before require_bullish can pass.",
    "options_flow.max_symbols_per_cycle": "Paid calls: at most this many candidates get a flow scan per cycle, best ranked first.",
    # -- edge score
    "edge.enabled": "Compute the weighted Unusual Whales edge score for candidates that reached the context stage.",
    "edge.rank_weight": "Weight of the edge score inside the composite context score used for ranking.",
    "edge.insider_days": "Lookback for the insiders feature (Form 4 buys vs sells).",
    "edge.congress_days": "Lookback for congressional trades.",
    "edge.analyst_days": "Lookback for analyst upgrades / downgrades / initiations.",
    "edge.dark_pool_min_premium": "Only off-exchange prints at least this large are read for direction.",
    "edge.high_short_float": "Short % of float (0.15 = 15 %) that counts as full squeeze fuel in the short-interest feature.",
    "edge.daily_cache_hours": "Facts that change once a day (short interest, filings, seasonality) are fetched at most this often.",
    # -- adaptive risk
    "adaptive.enabled": "Scale risk per trade with the realised R of recent trades: size up when the market is paying, down when it is not.",
    "adaptive.lookback_trades": "How many of the most recent closed trades are averaged.",
    "adaptive.min_trades": "Fewer closed trades than this and the multiplier stays at 1.0.",
    "adaptive.cold_avg_r": "Average R at or below which risk is cut.",
    "adaptive.cold_risk_mult": "Risk multiplier when cold (0.5 = half size).",
    "adaptive.hot_avg_r": "Average R at or above which risk is raised.",
    "adaptive.hot_risk_mult": "Risk multiplier when hot (1.25 = 25 % more).",
    "adaptive.max_risk_per_trade_pct": "Hard ceiling on risk per trade after the multiplier (0.01 = 1 %).",
    # -- reviewer
    "reviewer.provider": "Which AI provider answers (settings → AI providers): Gemini, OpenAI, Anthropic, xAI, a local Ollama... auto = the first provider with a key.",
    "reviewer.model": "Model name. Blank = the provider's default where it has one (gemini-3.5-flash, gpt-4o-mini). Pick from the live list the field offers once the provider has a key.",
    "reviewer.base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    "reviewer.timeout_seconds": "Give up on the model after this long; with fail_closed on, that skips the trade.",
    "reviewer.min_confidence": "gate modes: the verdict must be BUY with at least this confidence (0..1).",
    "reviewer.review_watchlist": "Also review watch plans parked for tomorrow (one model call each).",
    # -- schedule
    "schedule.premarket_enabled": "Run the pre-market gap screen at premarket_times (needs Unusual Whales or finviz).",
    "schedule.premarket_max_names": "Most gappers added to the arming list per pre-market scan.",
    "schedule.post_open_time": "NY time of the post-open whole-universe pass.",
    "schedule.focused_start": "NY time of the first focused pass.",
    "schedule.focused_stop_before_close_minutes": "Last focused pass ends this many minutes before the close.",
    "schedule.pace_min_session_fraction": "Before this share of the session has elapsed (0.08 ≈ 09:44) the volume projection is too noisy and raw volume is used.",
    "schedule.movers_enabled": "Sweep the day's top gainers on relative volume every movers_interval_minutes.",
    "schedule.movers_interval_minutes": "How often the movers screen runs.",
    "schedule.movers_start": "NY time of the first movers sweep.",
    "schedule.movers_stop_before_close_minutes": "Last movers sweep ends this many minutes before the close.",
    "schedule.movers_max_names": "Most names added to the arming list per movers sweep.",
    # -- insider scan
    "insider_scan.weekday": "Day of the week the scan runs (saturday).",
    "insider_scan.run_time": "NY time the scan runs.",
    "insider_scan.lookback_days": "Days of flow alerts read per scan (7 = the whole week).",
    "insider_scan.min_dte": "Ignore contracts expiring sooner than this many days (0-2 DTE is day-trading and lottery flow, not a bet on an event).",
    "insider_scan.max_dte": "Only contracts expiring within this many days - a few weeks out is the informed window; beyond ~45 is ordinary positioning.",
    "insider_scan.enrich_top": "How many top candidates get two extra Unusual Whales reads (market cap, week's option volume vs the 30-day average) before the final ranking. 0 = off (mega caps can then slip through the daily contract screen).",
    "insider_scan.min_ask_side_pct": "Share of premium paid at the ask (0.6 = 60 %): an aggressive buyer, not a seller.",
    "insider_scan.include_puts": "Also flag heavy put buying - bearish bets ahead of bad news are just as telling.",
    "insider_scan.min_market_cap": "Skip tickers below this market cap (micro caps are noisy). Blank = no floor.",
    "insider_scan.exclude_tickers": "Comma separated index / sector ETFs whose flow is routine hedging.",
    "insider_scan.contract_screen": "Also read the daily unusual-contract screen for each session (one paid call per day).",
    "insider_scan.provider": "Which AI provider answers for the catalyst analysis (settings → AI providers); auto = the first with a key.",
    "insider_scan.model": "Model name. Blank = the provider's default where it has one.",
    "insider_scan.base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    "insider_scan.timeout_seconds": "Give up on the model after this long; the ticker is then listed without an analysis.",
    "insider_scan.context_news_days": "Days of headlines given to the model for each flagged ticker.",
    # -- entry / learning extras
    "entry.failed_breakout_tolerance_pct": "Price must be back below the pivot by more than this fraction (0.005 = 0.5 %) before the failed-breakout exit fires.",
    "learning.shadow_hold_days": "Sessions a triggered shadow trade is followed to measure what it would have made.",
    "learning.review_weekday": "Day of the week the learning review runs.",
    "learning.review_time": "NY time the review runs.",
    "learning.provider": "Which AI provider writes the post-mortems (settings → AI providers); auto = the first with a key.",
    "learning.model": "Model name. Blank = the provider's default where it has one.",
    "learning.base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    "learning.timeout_seconds": "Give up on the model after this long; the rule-based post-mortem is kept.",
    "learning.max_post_mortems_per_run": "Most closed trades sent to the model per review (cost control).",
    # -- advisor
    "advisor.enabled": "Show the advisor page: describe the risk you want or ask for advice in plain English; the model proposes setting changes you accept one by one.",
    "advisor.provider": "Which AI provider answers the advisor (settings → AI providers); auto = the first with a key.",
    "advisor.model": "Model name. Blank = the provider's default where it has one. A stronger model is worth it here: it reasons over the whole settings map.",
    "advisor.base_url": "Advanced: endpoint override for this seat only. Blank = the provider's own endpoint.",
    "advisor.timeout_seconds": "Give up on the model after this long.",
    "advisor.max_history": "Earlier exchanges (your messages and its replies) kept in the prompt for context.",
    "advisor.max_changes": "Most setting changes accepted from one reply; the rest are dropped with a note.",
}

# The handful of dials worth a card at the top of the page.
QUICK_FIELDS: tuple[str, ...] = (
    "risk.risk_per_trade_pct", "risk.max_positions", "risk.max_position_pct",
    "management.stop_adr_mult", "management.partial_target_r", "management.trail_ma",
    "regime.enabled", "themes.enabled", "context.enabled",
    "options_flow.enabled", "edge.enabled", "edge.gate", "edge.threshold", "edge.min_coverage",
    "reviewer.enabled", "reviewer.mode", "reviewer.fail_closed",
    "schedule.tiered", "schedule.focused_interval_minutes", "insider_scan.enabled", "insider_scan.ai_enabled",
    "entry.mode", "entry.confirm_volume_ratio", "entry.failed_breakout_exit", "learning.enabled", "learning.auto_apply",
)

# Every setting that names an AI provider; its choices are the providers on
# the AI providers card (qmag.providers), not a fixed list.
PROVIDER_KEYS = ("reviewer.provider", "insider_scan.provider", "learning.provider", "advisor.provider",
                 "committee.bull_provider", "committee.bear_provider", "committee.risk_provider")

CHOICES: dict[str, tuple[str, ...]] = {
    "reviewer.mode": ("advisory", "gate", "gate_and_size"),
    "sentiment.source": ("csv",),
    "options_flow.provider": ("unusual_whales",),
    "insider_scan.weekday": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
    "entry.mode": ("confirmed", "resting", "hybrid"),
    "learning.review_weekday": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
}


def _field_kind(type_str: str) -> str:
    t = type_str.replace(" ", "")
    if t == "bool":
        return "bool"
    if t in ("int", "int|None"):
        return "int"
    if t in ("float", "float|None"):
        return "float"
    if t.startswith("list["):
        return "list"
    if t.startswith("dict[str,float]"):
        return "weights"
    if t.startswith("dict["):
        return "yaml"
    return "str"


def _optional(type_str: str) -> bool:
    return "None" in type_str


def provider_choices() -> list[str]:
    from .providers import registry

    return registry().choices()


def describe_config(cfg: StrategyConfig) -> list[dict[str, Any]]:
    """Sections and fields of the strategy config as the form needs them."""
    defaults = StrategyConfig()
    sections = []
    for sf in fields(StrategyConfig):
        sub = getattr(cfg, sf.name)
        dflt = getattr(defaults, sf.name)
        doc = (type(sub).__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
        if doc.startswith(type(sub).__name__ + "("):  # dataclass default repr, not prose
            doc = SECTION_DOCS.get(sf.name, "")
        doc = re.sub(r"\s+", " ", doc).replace("``", "")
        first = re.split(r"(?<=[.!?])\s", doc, maxsplit=1)[0]
        doc = SECTION_SUMMARY.get(sf.name) or (first if len(first) > 20 else doc)
        items = []
        for f in fields(sub):
            key = f"{sf.name}.{f.name}"
            kind = _field_kind(str(f.type))
            value = getattr(sub, f.name)
            default = getattr(dflt, f.name)
            if kind == "list":
                shown = ", ".join(str(x) for x in (value or []))
            elif kind == "yaml":
                shown = yaml.safe_dump(value, sort_keys=False).strip() if value else ""
            elif kind == "weights":
                shown = value or {}
            elif value is None:
                shown = ""
            else:
                shown = value
            items.append({
                "key": key, "name": f.name, "label": f.name.replace("_", " "), "kind": kind, "optional": _optional(str(f.type)),
                "value": shown, "default": default, "changed": value != default, "help": FIELD_HELP.get(key, ""),
                "choices": provider_choices() if key in PROVIDER_KEYS else list(CHOICES.get(key, ())), "quick": key in QUICK_FIELDS,
            })
        sections.append({
            "name": sf.name, "label": SECTION_LABELS.get(sf.name, sf.name), "doc": doc, "fields": items,
            "changed": sum(1 for i in items if i["changed"]),
        })
    return sections


def quick_fields(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {f["key"]: dict(f, section=s["label"]) for s in sections for f in s["fields"]}
    return [by_key[k] for k in QUICK_FIELDS if k in by_key]


# Every place a language model is used, with the keys that pick the provider
# and model. The per-seat ``base_url`` override stays in the full parameter
# list; the card itself only offers the providers from the AI providers card.
LLM_USES: tuple[dict[str, Any], ...] = (
    {"key": "reviewer", "label": "Trade reviewer", "what": "BUY / SELL / HOLD verdict on every candidate before an order (advisory, gate or gate-and-size).",
     "enabled": "reviewer.enabled", "provider": "reviewer.provider", "model": "reviewer.model", "base_url": "reviewer.base_url", "connection": "llm_reviewer"},
    {"key": "advisor", "label": "Desk advisor", "what": "The plain-English manager page: reads your request and the settings map, proposes changes you accept one by one.",
     "enabled": "advisor.enabled", "provider": "advisor.provider", "model": "advisor.model", "base_url": "advisor.base_url", "connection": "llm_advisor"},
    {"key": "insider_scan", "label": "Insider-flow analyst", "what": "Possible catalyst / speculation per ticker flagged by the weekly unusual-options scan.",
     "enabled": "insider_scan.ai_enabled", "provider": "insider_scan.provider", "model": "insider_scan.model", "base_url": "insider_scan.base_url", "connection": "llm_insider"},
    {"key": "learning", "label": "Post-mortem coach", "what": "What worked / what failed per closed trade, feeding the learning layer.",
     "enabled": "learning.llm_enabled", "provider": "learning.provider", "model": "learning.model", "base_url": "learning.base_url", "connection": "llm_learning"},
    {"key": "committee_bull", "label": "Committee · bull researcher", "group": "committee", "seat": "Seat 1 of 3",
     "what": "Argues FOR each plan from the same facts the engine saw. Three separate calls per plan; give the seats different vendors for a genuine debate.",
     "enabled": "committee.enabled", "provider": "committee.bull_provider", "model": "committee.bull_model", "base_url": "committee.bull_base_url", "connection": "llm_committee"},
    {"key": "committee_bear", "label": "Committee · bear researcher", "group": "committee", "seat": "Seat 2 of 3",
     "what": "Argues AGAINST the plan and, with debate on, rebuts the bull case point by point.",
     "enabled": None, "provider": "committee.bear_provider", "model": "committee.bear_model", "base_url": "committee.bear_base_url", "connection": "llm_committee"},
    {"key": "committee_risk", "label": "Committee · risk chair", "group": "committee", "seat": "Seat 3 of 3",
     "what": "Reads the facts and both cases, rules take / reduce / reject with a size multiplier. Veto and debate switches: committee section below.",
     "enabled": None, "provider": "committee.risk_provider", "model": "committee.risk_model", "base_url": "committee.risk_base_url", "connection": "llm_committee",
     "extras": ("committee.can_veto", "committee.debate")},
)


def llm_uses(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The LLM uses with their live field descriptions, for the models card on the settings page."""
    by_key = {f["key"]: dict(f, section=s["label"]) for s in sections for f in s["fields"]}
    out = []
    for use in LLM_USES:
        row = dict(use)
        fields = {}
        for slot in ("enabled", "provider", "model"):
            k = use.get(slot)
            if k and k in by_key:
                fields[slot] = by_key[k]
        if "model" not in fields:
            continue
        row["fields"] = fields
        row["extras"] = [by_key[k] for k in use.get("extras", ()) if k in by_key]
        out.append(row)
    return out


def _coerce(kind: str, raw: str, optional: bool, key: str):
    raw = (raw or "").strip()
    if kind == "bool":
        return raw.lower() in ("1", "true", "on", "yes")
    if raw == "":
        if optional or kind in ("list", "yaml"):
            return None if kind not in ("list",) else []
        raise ValueError(f"{key}: a value is required")
    try:
        if kind == "int":
            return int(float(raw))
        if kind == "float":
            return float(raw)
    except ValueError:
        raise ValueError(f"{key}: '{raw}' is not a number") from None
    if kind == "list":
        return [x.strip() for x in raw.split(",") if x.strip()]
    if kind == "yaml":
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError(f"{key}: not valid YAML ({exc})") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError(f"{key}: expected a mapping")
        return parsed
    return raw


def config_from_form(form: dict[str, str], base: StrategyConfig) -> StrategyConfig:
    """Rebuild a config from submitted form fields.

    Only keys present in ``form`` are changed; everything else keeps
    ``base``'s value. (The page posts a hidden ``0`` before every checkbox so
    an unticked box still arrives.) Raises ``ValueError`` with a readable
    message on the first bad value.
    """
    raw = base.to_dict()
    for sf in fields(StrategyConfig):
        sub = getattr(base, sf.name)
        for f in fields(sub):
            key = f"{sf.name}.{f.name}"
            kind = _field_kind(str(f.type))
            if kind == "weights":
                prefix = key + "."
                weights = dict(raw[sf.name][f.name] or {})
                touched = False
                for k, v in form.items():
                    if k.startswith(prefix):
                        touched = True
                        name = k[len(prefix):]
                        try:
                            weights[name] = float(v) if str(v).strip() != "" else 0.0
                        except ValueError:
                            raise ValueError(f"{k}: '{v}' is not a number") from None
                        if weights[name] < 0:
                            raise ValueError(f"{k}: weights cannot be negative")
                if touched:
                    raw[sf.name][f.name] = weights
                continue
            if key in form:
                raw[sf.name][f.name] = _coerce(kind, form[key], _optional(str(f.type)), key)
    cfg = StrategyConfig.from_dict(raw)
    validate_config(cfg)
    return cfg


def _valid_hhmm(value: object) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value or "")))


def validate_config(cfg: StrategyConfig) -> None:
    """Sanity limits that the dataclasses themselves do not enforce."""
    r, m, e, rv = cfg.risk, cfg.management, cfg.edge, cfg.reviewer
    import math
    def finite_tree(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                finite_tree(item, f"{path}.{key}" if path else key)
        elif isinstance(value, (list, tuple)):
            for item in value:
                finite_tree(item, path)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
    finite_tree(cfg.to_dict())
    problems = []
    a = cfg.autonomy
    if not 0 < a.canary_risk_fraction <= 1:
        problems.append("autonomy.canary_risk_fraction must be > 0 and <= 1")
    if a.min_forward_days < 20 or a.min_forward_trades < 30 or a.min_model_records < 80:
        problems.append("autonomy evidence minimums are 20 sessions, 30 prospective trades and 80 model records")
    if not 1 <= a.max_candidates <= 20 or a.min_history_days < 250:
        problems.append("autonomy needs 250+ history days and 1..20 candidates")
    if not 0 < a.rollback_drawdown <= a.max_drawdown <= 0.3 or a.minimum_return_lift < 0:
        problems.append("autonomy drawdowns must satisfy 0 < rollback <= maximum <= 30%; lift must be nonnegative")
    if a.canary_trades < 20 or a.max_trial_days < 30 or a.retention_days < 7:
        problems.append("autonomy needs 20+ canary trades, 30+ trial days and 7+ retention days")
    if a.research_weekday not in CHOICES["insider_scan.weekday"] or not _valid_hhmm(a.research_time):
        problems.append("autonomy research schedule must specify a weekday and HH:MM time")
    if not 0 < r.risk_per_trade_pct <= 0.1:
        problems.append("risk.risk_per_trade_pct must be between 0 and 0.1 (10 %)")
    if not 0 < r.max_position_pct <= 1:
        problems.append("risk.max_position_pct must be between 0 and 1")
    if r.max_positions < 1:
        problems.append("risk.max_positions must be at least 1")
    if r.starting_equity <= 0:
        problems.append("risk.starting_equity must be positive")
    if not 0 <= r.max_portfolio_heat_pct <= 0.5:
        problems.append("risk.max_portfolio_heat_pct must be between 0 (off) and 0.5")
    if not 0 <= r.daily_loss_limit_pct <= 0.5:
        problems.append("risk.daily_loss_limit_pct must be between 0 (off) and 0.5")
    if r.max_positions_per_theme < 0:
        problems.append("risk.max_positions_per_theme must be 0 (off) or more")
    if m.stop_adr_mult <= 0:
        problems.append("management.stop_adr_mult must be positive")
    if not 0 < m.partial_fraction < 1:
        problems.append("management.partial_fraction must be between 0 and 1")
    if m.time_stop_days < 0:
        problems.append("management.time_stop_days must be 0 (off) or positive")
    if not -1 <= e.threshold <= 1:
        problems.append("edge.threshold must be between -1 and +1")
    if not 0 <= e.min_coverage <= 1:
        problems.append("edge.min_coverage must be between 0 and 1")
    if rv.mode not in CHOICES["reviewer.mode"]:
        problems.append("reviewer.mode must be advisory, gate or gate_and_size")
    if not 0 <= rv.min_confidence <= 1:
        problems.append("reviewer.min_confidence must be between 0 and 1")
    unknown = set(e.weights) - set(DEFAULT_EDGE_WEIGHTS)
    if unknown:
        problems.append("edge.weights has unknown features: " + ", ".join(sorted(unknown)))
    sch, ins = cfg.schedule, cfg.insider_scan
    for key, value in (("schedule.focused_start", sch.focused_start), ("schedule.movers_start", sch.movers_start),
                       ("schedule.post_open_time", sch.post_open_time), ("insider_scan.run_time", ins.run_time),
                       *[("schedule.premarket_times", t) for t in sch.premarket_times]):
        if not _valid_hhmm(value):
            problems.append(f"{key}: '{value}' is not a HH:MM time")
    if sch.focused_interval_minutes < 1 or sch.movers_interval_minutes < 1:
        problems.append("schedule intervals must be at least 1 minute")
    if not 0 < sch.arming_distance_pct <= 0.5 or not 0 < sch.trigger_distance_pct <= 0.5:
        problems.append("schedule.arming_distance_pct / trigger_distance_pct must be between 0 and 0.5")
    if ins.weekday not in CHOICES["insider_scan.weekday"]:
        problems.append("insider_scan.weekday must be a weekday name")
    if not 0 <= ins.min_flag_score <= 10:
        problems.append("insider_scan.min_flag_score must be between 0 and 10")
    if ins.max_pages < 1 or ins.max_flagged < 1:
        problems.append("insider_scan.max_pages and max_flagged must be at least 1")
    en, ln = cfg.entry, cfg.learning
    if en.mode not in CHOICES["entry.mode"]:
        problems.append("entry.mode must be confirmed, resting or hybrid")
    if not _valid_hhmm(en.resting_from):
        problems.append(f"entry.resting_from: '{en.resting_from}' is not a HH:MM time")
    if not 0 < en.confirm_volume_ratio <= 5:
        problems.append("entry.confirm_volume_ratio must be between 0 and 5")
    if not 0 <= en.opening_range_minutes <= 120:
        problems.append("entry.opening_range_minutes must be between 0 and 120")
    if en.failed_breakout_days < 0 or not 0 <= en.failed_breakout_tolerance_pct <= 0.1:
        problems.append("entry.failed_breakout_days must be >= 0 and tolerance between 0 and 0.1")
    if not _valid_hhmm(ln.review_time):
        problems.append(f"learning.review_time: '{ln.review_time}' is not a HH:MM time")
    if ln.review_weekday not in CHOICES["learning.review_weekday"]:
        problems.append("learning.review_weekday must be a weekday name")
    if ln.min_trades < 3:
        problems.append("learning.min_trades must be at least 3")
    if ln.min_lift_r < 0 or ln.shadow_max_days < 1 or ln.shadow_hold_days < 1:
        problems.append("learning.min_lift_r must be >= 0 and shadow day limits at least 1")
    if problems:
        raise ValueError("; ".join(problems))


def config_from_yaml(text: str) -> StrategyConfig:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the strategy YAML must be a mapping of sections")
    cfg = StrategyConfig.from_dict(raw)
    validate_config(cfg)
    return cfg


def config_yaml(cfg: StrategyConfig) -> str:
    return yaml.safe_dump(cfg.to_dict(), sort_keys=False)
