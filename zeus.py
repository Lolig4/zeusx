#!/usr/bin/env python3
"""
Client für ISGUS ZEUS X. -- ASP.NET WebForms mit
DevExpress, alles läuft über Callbacks auf einer Workspace-Seite.
"""

from __future__ import annotations

import argparse
import functools
import getpass
import json
import os
import re
import stat
import sys
import time
from datetime import date, datetime, timedelta
from html import escape, unescape
from urllib.parse import urljoin

import requests

BASE_URL = "https://saas.isgus.de"
LOGON = "Environment/Account/LogOn.aspx"

# Alles neben dem Skript -- Cookies also im Projektordner.
DATA = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DATA, "config.json")
CONFIG_EXAMPLE = os.path.join(DATA, "config.example.json")
COOKIES = os.path.join(DATA, "cookies.json")
LAST_REQUEST = os.path.join(DATA, "last_request")

SESSION_TIMEOUT = 1800

# Die einzigen sechs Muster, die wir am Markup brauchen.
RE_TOKEN = re.compile(r'id="(__VIEWSTATE|__VIEWSTATEGENERATOR|__EVENTVALIDATION)"'
                      r'[^>]*value="([^"]*)"')
RE_WIDGET = re.compile(r"RegisterWidget\('WidgetId:(\w+).*?WidgetType:(\w+), "
                       r"PartId:([0-9a-f-]*)")
RE_WORKSPACE = re.compile(r'data-workspaceid="([0-9a-f-]{36})" value="([^"]*)"')
RE_JSON = re.compile(r"OnCaller\(s,\s*e,\s*(\{.*\})\);", re.DOTALL)
RE_ACCOUNT = re.compile(r'item-description[^>]*>([^<]*)<.*?element-value[^>]*>([^<]*)<',
                        re.DOTALL)
RE_BUTTON = re.compile(
    r"CallbackWidget\(null, null, &#39;(WidgetType: Terminal.*?)&#39;"
    r".*?terminal-button-text\">([^<]*)<", re.DOTALL)

WIDGET_STATE = '{"data":"12|#|uiWidgetState|15|##"}'


class ZeusError(Exception):
    """Alles, was schiefgehen kann."""

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}

def config() -> dict:
    """Inhalt von config.json; fehlt sie, ein leeres dict."""
    return _read_json(CONFIG_FILE)

def save_config(settings: dict) -> None:
    """Schreibt config.json. Rechte eng, es kann ein Passwort drinstehen."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
    os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)

@functools.cache
def tenant() -> str:
    """Mandant -- ausschliesslich aus config.json, sonst Abfrage.

    Die Eingabe wird gemerkt; fehlt die Datei, entsteht sie aus der Vorlage.
    """
    settings = config()
    value = (settings.get("tenant") or "").strip()
    if value:
        return value

    try:
        value = input("Mandant: ").strip()
    except EOFError:   # kein Terminal, z.B. in einer Pipe
        value = ""
    if not value:
        raise ZeusError(f"Kein Mandant angegeben -- in "
                        f"{os.path.basename(CONFIG_FILE)} eintragen.")
    save_config({**(settings or _read_json(CONFIG_EXAMPLE)), "tenant": value})
    return value

def root_url() -> str:
    """Basis aller Pfade. Erst hier faellt die Mandanten-Abfrage an."""
    return urljoin(BASE_URL, f"{tenant()}/")

def save_cookies(session: requests.Session) -> None:
    with open(COOKIES, "w", encoding="utf-8") as handle:
        json.dump(session.cookies.get_dict(), handle, indent=2)
    os.chmod(COOKIES, stat.S_IRUSR | stat.S_IWUSR)

def load_session() -> requests.Session:
    if not os.path.exists(COOKIES):
        raise ZeusError("Nicht angemeldet -- erst 'zeus.py login' ausfuehren.")
    session = requests.Session()
    with open(COOKIES, encoding="utf-8") as handle:
        stored = json.load(handle)
    for name, value in stored.items():
        session.cookies.set(name, value, domain="saas.isgus.de", path="/")
    return session

def _touch() -> None:
    """Vermerkt die letzte Anfrage -- daraus errechnet sich die Restzeit."""
    with open(LAST_REQUEST, "w", encoding="utf-8") as handle:
        handle.write(str(time.time()))

def session_remaining() -> float | None:
    """Restzeit in Sekunden. Stellt bewusst keine Anfrage -- die wuerde die
    Frist verlaengern. None: noch nichts vermerkt."""
    try:
        with open(LAST_REQUEST, encoding="utf-8") as handle:
            last = float(handle.read())
    except (OSError, ValueError):
        return None
    return SESSION_TIMEOUT - (time.time() - last)

def extend_session(session: requests.Session) -> None:
    """'Extend session' der Statusleiste: `c0:KeepAlive` an den
    Master-Callback."""
    callback(session, open_page(session), "ctl00$MasterEventCallback",
             "c0:KeepAlive")

def open_page(session: requests.Session, path: str = "") -> dict:
    """Laedt eine Seite und liest ihre drei ASP.NET-Tokens."""
    response = session.get(urljoin(root_url(), path), timeout=30)
    if LOGON.lower() in response.url.lower():
        raise ZeusError("Session abgelaufen -- 'zeus.py login' erneut ausfuehren.")
    _touch()
    return {"url": response.url, "html": response.text,
            "tokens": dict(RE_TOKEN.findall(response.text))}

def callback(session: requests.Session, page: dict, target: str, param: str, extra: dict | None = None) -> str:
    """DevExpress-Callback. Alle uebrigen Felder des Browsers ignoriert der
    Server nachweislich -- nur __VIEWSTATE ist Pflicht."""
    data = {"__VIEWSTATE": page["tokens"]["__VIEWSTATE"]}
    data.update(extra or {})
    data["__CALLBACKID"] = target
    data["__CALLBACKPARAM"] = param
    response = session.post(page["url"], data=data, timeout=30)
    _touch()
    return response.text

# --- Widgets ---

def widgets(page: dict) -> list[dict]:
    """Widgets der Seite mit Id, Typ und Callback-Pfaden. Widgets in einem
    Part-Container brauchen ein Segment `p<8-hex-der-PartId>` im Pfad --
    fehlt es, laeuft der Callback wirkungslos ins Leere."""
    out = []
    for widget_id, kind, part in RE_WIDGET.findall(page["html"]):
        prefix = f"p{part.split('-')[0]}$" if part else ""
        outer = f"ctl00$zC${prefix}WsWiCb{widget_id}"
        out.append({"id": widget_id, "type": kind, "outer": outer,
                    "inner": f"{outer}$w{widget_id}$wT$wCb"})
    return out

def find_widget(page: dict, kind: str) -> dict:
    for widget in widgets(page):
        if widget["type"].casefold() == kind.casefold():
            return widget
    raise ZeusError(f"Kein Widget vom Typ {kind!r}. Vorhanden: "
                    + ", ".join(sorted(other["type"] for other in widgets(page))))

def workspace_with(session: requests.Session, kind: str) -> tuple[dict, dict]:
    """Sucht den Workspace, der ein Widget des gewuenschten Typs enthaelt."""
    page = open_page(session)
    try:
        return page, find_widget(page, kind)
    except ZeusError:
        pass
    for guid, _label in RE_WORKSPACE.findall(page["html"]):
        other = open_page(session, f"content/workspace.aspx?id={guid}")
        try:
            return other, find_widget(other, kind)
        except ZeusError:
            continue
    raise ZeusError(f"Kein Workspace enthaelt ein Widget vom Typ {kind!r}.")

def widget_call(session: requests.Session, page: dict, widget: dict, command: str) -> str:
    """Aktion INNERHALB eines Widgets -- laeuft ueber das innere Panel."""
    return callback(session, page, widget["inner"], f"c0:{command}",
                    {f"{widget['inner']}$uiWidgetState": WIDGET_STATE})

def prepare(session: requests.Session, page: dict, widget: dict) -> None:
    """Widget rendern und Daten neu holen. `Load` allein liefert den alten
    Stand -- `Refresh` ist der Aktualisieren-Knopf des Widgets."""
    callback(session, page, widget["outer"], f"c0:Load, WidgetId:{widget['id']}")
    widget_call(session, page, widget, "Refresh")


def grid_data(session: requests.Session, page: dict, widget: dict, grid: str, event: dict):
    """Ruft einen Gitter-Callback auf; die Nutzdaten sind JSON."""
    raw = callback(session, page,
                   f"{widget['inner']}$wCT{widget['id']}${grid}${grid}Callback",
                   "c0:" + json.dumps(event, separators=(",", ":")),
                   {f"{widget['inner']}$uiWidgetState": WIDGET_STATE})
    match = RE_JSON.search(raw)
    if not match:
        raise ZeusError(f"{grid} lieferte keine auswertbaren Daten.")
    # JavaScript-Literal, kein JSON: \' lehnt json.loads ab.
    return json.loads(match.group(1).replace("\\'", "'"))["Result"]

# --- Buchungen lesen ---

def _cells(session: requests.Session, long_range: bool) -> tuple[list, dict]:
    """Rohzellen des Buchungsgitters plus Uebersetzungstabelle."""
    kind, grid = (("PersonalBooking", "uiPersonalBookingGrid") if long_range
                  else ("BookingOverview", "uiBookingOverviewGrid"))
    page, widget = workspace_with(session, kind)
    prepare(session, page, widget)

    head = grid_data(session, page, widget, grid,
                     {"Event": "Create",
                      "Parameters": [{"Key": "Restore", "Value": False}]})
    parts = [head]

    # Grosses Gitter: `Create` liefert nur Monatsgruppen, die Zeilen kommen
    # mit `ExpandGroups` -- als Liste, ein Eintrag je Gruppe.
    groups = [group["Id"]
              for group in head.get("Data", {}).get("Top", {}).get("Grouping", [])]
    if groups:
        parts = grid_data(session, page, widget, grid,
                          {"Event": "ExpandGroups",
                           "Parameters": [{"Key": "ExpandGroups", "Value": groups},
                                          {"Key": "Restore", "Value": False}]})
        if not isinstance(parts, list):
            parts = [parts]

    cells, labels = [], {}
    for part in parts:
        data = part.get("Data", {})
        cells += data.get("Top", {}).get("Cells", [])
        for entry in data.get("Credentials", {}).get("Localization", []):
            labels.setdefault(entry["Key"], entry["Value"])
    return cells, labels

def punches(session: requests.Session, day: date | None = None, days: int | None = None) -> list[dict]:
    """Buchungen als flache, chronologische Liste. Das Tages-Widget deckt eine
    Woche ab; reicht das nicht, uebernimmt das grosse Gitter."""
    since = day or (date.today() - timedelta(days=(days or 1) - 1))  # noqa: DTZ011
    cells, labels = _cells(session, long_range=False)
    if cells and since < _day(min(str(cell["Key"]) for cell in cells)):
        cells, labels = _cells(session, long_range=True)
    return build_events(cells, labels, since, day)

def _cell_status(field: dict, labels: dict, value: str) -> str:
    """Der Tooltip wiederholt den Wert; uebrig bleibt der Bearbeitungsstand."""
    raw = _resolve(str(field.get("ToolTip", "")), labels).strip()
    rest = raw[len(value):]
    if value and raw.startswith(value) and rest[:1] in (" ", "("):
        return rest.strip()
    return raw if raw != value else ""

def build_events(cells: list, labels: dict, since: date, day: date | None = None) -> list[dict]:
    """Rohzellen -> flache Buchungsliste. Ohne Netz, damit pruefbar."""
    events = []
    no_time: dict[date, dict] = {}
    for cell in cells:
        cell_day = _day(str(cell["Key"]))
        if cell_day is None or cell_day < since or (day and cell_day != day):
            continue
        fields = {entry["Key"]: entry["Value"] for entry in cell["Value"]}
        texts = {name: _resolve(str(field.get("Text", "")), labels).strip()
                 for name, field in fields.items()}

        # Geklammerte Zeiten `(12:20 PM 1:07 PM)` wiederholen nur bereits
        # gelistete Buchungen.
        real = [(time_field, direction, absence_field)
                for time_field, direction, absence_field
                in (("Time1In", "KOMMT", "Absence1In"),
                    ("Time1Out", "GEHT", "Absence1Out"))
                if texts.get(time_field)
                and not (texts[time_field].startswith("(")
                         or texts[time_field].endswith(")"))]

        # Bei nur EINER Buchung steht die Abwesenheitskennung mitunter in der
        # Gegenspalte; bei zweien gehoert jede zu ihrer eigenen.
        fallback = ""
        if len(real) == 1:
            fallback = texts.get("Absence1In") or texts.get("Absence1Out", "")

        for time_field, direction, absence_field in real:
            events.append({
                "day": cell_day,
                "time": _time24(texts[time_field]),
                "direction": direction,
                "absence": texts.get(absence_field) or fallback,
                "status": _cell_status(fields.get(time_field, {}), labels,
                                       texts[time_field]),
                "reason": "",
            })
        if not real and texts.get("Remark"):
            no_time.setdefault(cell_day, {"day": cell_day, "time": "", "direction": "",
                                          "absence": "", "status": "",
                                          "reason": texts["Remark"]})

    # Ganztaegige Abwesenheiten nur ohne Stempelung -- sonst blosse Kommentare.
    stamped = {event["day"] for event in events}
    events += [entry for entry_day, entry in no_time.items()
               if entry_day not in stamped]
    return sorted(events, key=lambda event: (event["day"], event["time"] or "99:99"))

def _day(key: str) -> date | None:
    """Zeilenschluessel `JJMMTT_NNNN` -> Datum. Die Anzeigespalte waere
    sprachabhaengig (`8/14/2026` vs. `14.08.2026`)."""
    stamp = key[:6]
    if len(stamp) != 6 or not stamp.isdigit():
        return None
    try:
        return date(2000 + int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6]))
    except ValueError:
        return None

def _time24(text: str) -> str:
    """ZEUS liefert 12h oder 24h; ausgegeben wird immer 24h."""
    for pattern in ("%I:%M %p", "%H:%M", "%I:%M:%S %p", "%H:%M:%S"):
        try:
            # Zeitzonenlos gewollt: Stechuhr-Uhrzeit, kein Zeitpunkt.
            parsed = datetime.strptime(text.strip(), pattern)  # noqa: DTZ007
        except ValueError:
            continue
        return parsed.strftime("%H:%M")
    return text

def _resolve(text: str, labels: dict) -> str:
    """Platzhalter wie {e1} oder {1} ueber die mitgelieferte Tabelle aufloesen."""
    return unescape(re.sub(r"\{(\w+)\}",
                           lambda match: labels.get(match.group(1), match.group(0)),
                           text))

# --- Konten und Terminal ---

def accounts(session: requests.Session) -> list[tuple[str, str]]:
    """Saldo, Urlaubsguthaben, Wochendifferenz."""
    page, widget = workspace_with(session, "AccountInfo")
    callback(session, page, widget["outer"], f"c0:Load, WidgetId:{widget['id']}")
    raw = widget_call(session, page, widget, "Refresh")
    return [(unescape(label).strip(), unescape(value).strip())
            for label, value in RE_ACCOUNT.findall(raw)]

def options(session: requests.Session) -> list[dict]:
    """Buchungsoptionen aller Gruppen. Das Terminal zeigt nur eine, also wird
    durchgewechselt -- der Wechsel selbst kann nichts ausloesen."""
    page, widget = workspace_with(session, "Terminal")
    prepare(session, page, widget)
    markup = widget_call(session, page, widget, "Refresh")
    groups = re.findall(r"CurrentBookingGroup:([0-9a-f-]{36})", markup) or [None]

    found: dict[str, dict] = {}
    for guid in dict.fromkeys(groups):
        if guid:
            markup = widget_call(
                session, page, widget,
                f"WidgetType:Terminal, CurrentBookingGroup:{guid}")
        for command, label in RE_BUTTON.findall(markup):
            command = unescape(command)
            found.setdefault(unescape(label).strip(),
                             {"label": unescape(label).strip(), "command": command,
                              "group": guid})
    return list(found.values())

def book(session: requests.Session, label: str) -> None:
    """Löst eine Terminal-Buchung aus."""
    page, widget = workspace_with(session, "Terminal")
    prepare(session, page, widget)
    option = next((candidate for candidate in options(session)
                   if candidate["label"].casefold() == label.casefold()), None)
    if option is None:
        raise ZeusError(f"Keine Buchungsoption {label!r}.")

    # Nur die aktive Gruppe bucht; das Einsammeln oben hat sie verstellt.
    if option["group"]:
        widget_call(session, page, widget,
                    f"WidgetType:Terminal, CurrentBookingGroup:{option['group']}")

    widget_call(session, page, widget, option["command"])

# --- Anmeldung ---

def login(user: str, password: str, tan=None) -> requests.Session:
    """
    Dreistufige Anmeldung: Benutzer -> Passwort -> TAN.
    """
    session = requests.Session()
    page = open_page_raw(session, LOGON)

    def step(fields: dict) -> dict:
        data = {"__EVENTTARGET": "", "__EVENTARGUMENT": "", "DXScript": "", "DXCss": "",
                "uiBookingReceiptPopupState":
                    '{&quot;windowsState&quot;:'
                    '&quot;0:0:-1:0:0:0:-10000:-10000:1:0:0:0&quot;}',
                "stateField": _timezone_state()}
        data.update(page["tokens"])
        data.update(fields)
        response = session.post(page["url"], data=data, timeout=30,
                                headers={"Referer": page["url"], "Origin": BASE_URL})
        return {"url": response.url, "html": response.text,
                "tokens": dict(RE_TOKEN.findall(response.text))}

    page = step({"uiUserName": user, "uiUserName$State": _dx_state(user),
                 "uiPassword": "", "uiPassword$State": _dx_state(""),
                 "uiNextButton": "Continue"})
    if 'name="uiLogOnButton"' not in page["html"]:
        raise ZeusError(f"Benutzername {user!r} wurde nicht akzeptiert.")

    if 'name="uiSecurityToken"' not in page["html"]:
        page = step({"uiUserName": user, "uiUserName$State": _dx_state(user),
                     "uiPassword": password, "uiPassword$State": _dx_state(password),
                     "uiLogOnButton": "Login"})

    if 'name="uiSecurityToken"' in page["html"]:
        code = (tan or (lambda: getpass.getpass("Sicherheitscode (TAN): ")))().strip()
        if not code:
            raise ZeusError("Keine TAN eingegeben.")
        page = step({"uiSecurityToken": code, "uiSecurityToken$State": _dx_state(code),
                     "uiLogOnButton": "Login"})

    if LOGON.lower() in page["url"].lower():
        raise ZeusError("Anmeldung fehlgeschlagen. Vorsicht: ZEUS sperrt nach "
                        "mehreren Fehlversuchen.")
    _touch()   # der Login startet die Frist -- open_page_raw/step vermerken nichts
    save_cookies(session)
    return session

def open_page_raw(session: requests.Session, path: str) -> dict:
    """Wie open_page, aber ohne Anmelde-Prüfung (fuer die Anmeldemaske selbst)."""
    response = session.get(urljoin(root_url(), path), timeout=30)
    return {"url": response.url, "html": response.text,
            "tokens": dict(RE_TOKEN.findall(response.text))}

def _dx_state(value: str) -> str:
    """`<name>$State` zu jedem Eingabefeld. Erst JSON, dann HTML-escapen --
    andersherum zerlegt ein `"` im Passwort das Feld."""
    return escape(json.dumps({"rawValue": value}, ensure_ascii=False,
                             separators=(",", ":")), quote=True)

def _timezone_state() -> str:
    """`stateField`: Zeitzone des Clients. Relevant für Buchungen."""
    now = time.localtime()
    dst = now.tm_isdst > 0
    minutes = -(time.altzone if dst else time.timezone) // 60
    suffix = "DST" if dst else ""
    return f'{{"data":"12|#|Offset|2|3|{minutes}{suffix}|8|1|1#"}}'

# --- Kommandozeile ---

BLUE, GREEN, RESET = "\033[34m", "\033[32m", "\033[0m"
COLORS = {"inserted": BLUE, "eingefügt": BLUE, "approved": GREEN, "genehmigt": GREEN}

def _colorize(status: str) -> str:
    """Stand kommt je nach Sprache englisch oder deutsch."""
    if not status or not sys.stdout.isatty():
        return status
    color = COLORS.get(status.strip("() ").casefold())
    return f"{color}{status}{RESET}" if color else status

def show(events: list[dict]) -> None:
    if not events:
        print("Keine Buchungen.")
        return
    width = max(len(event["time"]) for event in events) or 5
    absence_width = max(len(event["absence"]) for event in events)
    distinct_days = {event["day"] for event in events}
    if len(distinct_days) == 1:
        print(f"Buchungen {events[0]['day'].strftime('%d.%m.%Y')}\n")
    last_day = None
    for event in events:
        if len(distinct_days) > 1 and event["day"] != last_day:
            last_day = event["day"]
            separator = "" if event is events[0] else "\n"
            print(f"{separator}{event['day'].strftime('%d.%m.%Y')}")
        if event["reason"]:
            line = f"  {'--':<{width}}  {event['reason']}"
        else:
            line = (f"  {event['time']:<{width}}  {event['direction']:<5}  "
                    f"{event['absence']:<{absence_width}}")
        print(f"{line}  {_colorize(event['status'])}".rstrip())

def _hms(seconds: float) -> str:
    """Sekunden -> hh:mm:ss wie in der ZEUS-Statusleiste."""
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `add -h` listet die echten Optionen -- argparse' Hilfe ist statisch.
    if argv[:1] == ["add"] and {"-h", "--help"} & set(argv):
        try:
            available = options(load_session())
        except ZeusError as error:
            print(f"Fehler: {error}", file=sys.stderr)
            return 1
        print("Aufruf: zeus.py add OPTION\n\nVerfuegbare Buchungsoptionen:")
        for option in available:
            print(f"  {option['label']}")
        return 0

    parser = argparse.ArgumentParser(prog="zeus.py", description="ISGUS ZEUS X Client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cmd = sub.add_parser("login", help="Anmelden (Benutzer, Passwort, TAN)")

    cmd = sub.add_parser("punches", help="Buchungen anzeigen (Default: heute)")
    cmd.add_argument("days", nargs="?", type=int, metavar="TAGE", help="die letzten N Tage, z.B. 'punches 2'")
    cmd.add_argument("--date", metavar="JJJJ-MM-TT", help="ein bestimmter Tag")
    cmd.add_argument("--json", action="store_true")

    sub.add_parser("session", help="Restzeit bis zur Abmeldung anzeigen")
    sub.add_parser("extend", help="Sitzung verlaengern ('Extend session')")
    sub.add_parser("accounts", help="Saldo und Urlaubsguthaben")

    cmd = sub.add_parser("add", help="Buchung hinzufügen ('-h' listet die Optionen)")
    cmd.add_argument("label")

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ZeusError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1

def _run(args: argparse.Namespace) -> int:
    if args.cmd == "login":
        settings = config()
        user = settings.get("user") or input("Benutzername: ").strip()
        password = settings.get("password") or getpass.getpass("Passwort: ")
        login(user, password)
        print(f"Angemeldet um {time.strftime('%d.%m.%Y %H:%M')}. "
              f"Abmeldung in {_hms(session_remaining())}")
        return 0

    # Ohne Anfrage, sonst wuerde das Nachsehen die Frist verlaengern.
    if args.cmd == "session":
        remaining = session_remaining()
        if remaining is None:
            print("Keine Anfrage vermerkt -- Restzeit unbekannt.")
            return 1
        if remaining <= 0:
            print(f"Abgelaufen (seit {_hms(-remaining)}).")
            return 1
        print(f"Abmeldung in {_hms(remaining)}")
        return 0

    session = load_session()

    if args.cmd == "extend":
        extend_session(session)
        print(f"Verlaengert. Abmeldung in {_hms(session_remaining())}")
        return 0

    if args.cmd == "punches":
        try:
            day = date.fromisoformat(args.date) if args.date else None
        except ValueError:
            print("Datum bitte als JJJJ-MM-TT angeben.", file=sys.stderr)
            return 2
        if args.days is not None and args.days < 1:
            print("Die Anzahl Tage muss mindestens 1 sein.", file=sys.stderr)
            return 2
        events = punches(session, day, args.days)
        if args.json:
            print(json.dumps([{**event, "day": event["day"].isoformat()}
                              for event in events], indent=2, ensure_ascii=False))
        else:
            show(events)
        return 0

    if args.cmd == "accounts":
        entries = accounts(session)
        width = max((len(label) for label, _ in entries), default=0)
        for label, value in entries:
            print(f"{label:<{width}}  {value:>8}")
        return 0

    if args.cmd == "add":
        book(session, args.label)
        # Der Tagesstand ist der einzig verlaessliche Nachweis.
        print(f"{args.label!r} abgeschickt. Stand danach:\n")
        show(punches(session))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
