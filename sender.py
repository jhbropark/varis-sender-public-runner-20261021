#!/usr/bin/env python3
"""varis.kr 이메일 시퀀스 발송기 (로컬 + GitHub Actions 겸용).
연락처(CSV 파일 또는 published Google Sheet CSV URL)를 읽어 3단계 시퀀스를 발송한다.
상태는 깃 친화적 JSON(state.json)에 저장하고, 민감정보는 환경변수로 주입한다.
이 버전은 메일마다 SMTP를 새로 연결하여 연결 끊김(Daum 등)에서 자동 복구한다.

  python sender.py sync
  python sender.py run --dry-run
  python sender.py run
  python sender.py status
  python sender.py mark a@b.com replied
옵션: --config PATH (기본 config.yaml)
환경변수(설정값 우선): SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USER SMTP_PASSWORD FROM_EMAIL
"""
import argparse, csv, io, json, os, sys, time, smtplib, ssl, re, urllib.request, urllib.parse, base64, html, mimetypes, glob
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

try:
    import yaml
except ImportError:
    print("PyYAML 필요: pip install -r requirements.txt", file=sys.stderr); sys.exit(1)

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def _utcnow(): return datetime.now(timezone.utc)
def now(): return _utcnow().isoformat()

# ---- 교차 캠페인 전역 안전장치 (cross_campaign_dedupe + global_daily_cap 옵트인) ----
KST = timezone(timedelta(hours=9))  # Asia/Seoul 고정 오프셋 (zoneinfo/tzdata 불필요)
STOP_STATUSES = {"optout", "replied", "done", "review_required"}

class GuardError(Exception):
    """전역 검사 불가/불확실 → fail-closed (발송 안 함). 메시지에 주소를 넣지 않는다."""

def guard_settings(cfg):
    """None=미적용(기존 동작). int=전역 일일 상한. 설정이 모호하면 GuardError."""
    d, c = cfg.get("cross_campaign_dedupe"), cfg.get("global_daily_cap")
    if c is None and d in (None, False):
        return None
    if d is not True or type(c) is not int or c < 1:
        raise GuardError("invalid_guard_config")
    return c

def _g_email(v):
    if not isinstance(v, str) or not v.strip():
        raise GuardError("malformed_email_field")
    return v.strip().lower()

def _g_ts(v):
    if not isinstance(v, str):
        raise GuardError("malformed_timestamp")
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise GuardError("malformed_timestamp")
    if dt.tzinfo is None:
        raise GuardError("timestamp_without_timezone")
    return dt.astimezone(timezone.utc)

class GlobalGuard:
    def __init__(self, cfg, cur_state, cap):
        self.cap = cap
        self.success, self.stopped, self.attempts = set(), set(), set()
        cur_abs = os.path.abspath(state_path(cfg))
        docs = []
        for p in sorted(glob.glob(os.path.join(os.path.dirname(cur_abs), "state*.json"))):
            try:
                with open(p, encoding="utf-8") as f:
                    docs.append((os.path.basename(p), json.load(f)))
            except (OSError, ValueError):
                raise GuardError("unreadable_state_file:" + os.path.basename(p))
        docs.append(("<memory>", cur_state))
        for name, d in docs:
            if not (isinstance(d, dict) and isinstance(d.get("contacts", {}), dict) and isinstance(d.get("log", []), list)):
                raise GuardError("malformed_state_structure:" + name)
            for em, c in d.get("contacts", {}).items():
                e = _g_email(em)
                if not isinstance(c, dict) or not isinstance(c.get("status"), str):
                    raise GuardError("malformed_contact:" + name)
                if c["status"] in STOP_STATUSES:
                    self.stopped.add(e)
            for l in d.get("log", []):
                if not isinstance(l, dict) or type(l.get("ok")) is not bool:
                    raise GuardError("malformed_log_entry:" + name)
                step = l.get("step")
                if type(step) is not int:
                    raise GuardError("malformed_log_entry:" + name)
                e, dt = _g_email(l.get("email")), _g_ts(l.get("sent_at"))
                self.attempts.add((e, step, dt))
                if l["ok"]:
                    self.success.add(e)
        self.today = _utcnow().astimezone(KST).date()
        self.used = sum(1 for (_, _, dt) in self.attempts if dt.astimezone(KST).date() == self.today)

    def remaining(self): return max(0, self.cap - self.used)

    def block_reason(self, email):
        e = email.strip().lower()
        if e in self.stopped: return "global_stop_status"
        if e in self.success: return "prior_success_any_campaign"
        return None

    def record(self, email, step, sent_at):
        """성공/실패 무관 시도 1건 기록 → remaining 감소(중복키는 1회만)."""
        key = (email.strip().lower(), step, _g_ts(sent_at))
        if key not in self.attempts:
            self.attempts.add(key)
            if key[2].astimezone(KST).date() == self.today:
                self.used += 1

ENV_KEYS = {"smtp_host":"SMTP_HOST","smtp_port":"SMTP_PORT","smtp_security":"SMTP_SECURITY",
            "smtp_user":"SMTP_USER","smtp_password":"SMTP_PASSWORD","from_email":"FROM_EMAIL"}

def load_cfg(path):
    p = path if os.path.isabs(path) else os.path.join(HERE, path)
    if not os.path.exists(p):
        print(f"{path} 없음. config.example.yaml 을 복사해 채우세요.", file=sys.stderr); sys.exit(1)
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for k, env in ENV_KEYS.items():
        v = os.environ.get(env)
        if v not in (None, ""):
            cfg[k] = int(v) if k == "smtp_port" else v
    return cfg

def state_path(cfg): return os.path.join(HERE, cfg.get("state_file", "state.json"))
def load_state(cfg):
    p = state_path(cfg)
    s = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    s.setdefault("contacts", {}); s.setdefault("log", [])
    return s
def save_state(cfg, s):
    with open(state_path(cfg), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def read_csv_text(text):
    rows = []; reader = csv.DictReader(io.StringIO(text))
    cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
    def pick(row, *keys):
        for k in keys:
            if k in cols and (row.get(cols[k]) or "").strip(): return row[cols[k]].strip()
        return ""
    for row in reader:
        rows.append((pick(row,"name"), pick(row,"email").lower(),
                     pick(row,"segment","group"),
                     pick(row,"mailing_status"),
                     pick(row,"verification_status")))
    return rows
def fetch_contacts(cfg):
    urls = cfg.get("contacts_url")
    if urls:
        if isinstance(urls, str):
            urls = [urls]
        import urllib.request
        all_rows = []
        for url in urls:
            if not url.strip(): continue
            req = urllib.request.Request(url, headers={"User-Agent":"varis-sender"})
            with urllib.request.urlopen(req, timeout=30) as r:
                all_rows.extend(read_csv_text(r.read().decode("utf-8","replace")))
        return all_rows
    path = os.path.join(HERE, cfg.get("contacts_file","contacts.csv"))
    if not os.path.exists(path):
        print(f"{path} 없음. CSV 내보내거나 contacts_url 을 설정하세요.", file=sys.stderr); sys.exit(1)
    return read_csv_text(open(path, encoding="utf-8-sig").read())
def load_suppress(cfg):
    s = set(); p = os.path.join(HERE, cfg.get("suppress_file","suppress.csv"))
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "@" in line: s.add(line.strip().lower())
    return s

def cmd_sync(cfg):
    s = load_state(cfg); added = skipped = updated = 0
    require_verified_public = bool(cfg.get("require_verified_public", False))
    allowed_verification = {"official_public_page_email"}
    for name, email, seg, mailing_status, verification_status in fetch_contacts(cfg):
        if not EMAIL_RE.match(email): skipped += 1; continue
        hunter_deliverable = verification_status.startswith("hunter_email_verifier_deliverable") or verification_status.startswith("hunter_email_verifier_valid")
        if require_verified_public and (mailing_status != "eligible_pending_send" or (verification_status not in allowed_verification and not hunter_deliverable)):
            skipped += 1; continue
        if email in s["contacts"]:
            old_seg = s["contacts"][email].get("segment")
            if old_seg != seg:
                s["contacts"][email]["segment"] = seg
                updated += 1
            continue
        s["contacts"][email] = {"name":name,"segment":seg,"step":0,"status":"active",
                                "last_sent_at":None,"created_at":now()}
        added += 1
    save_state(cfg, s)
    print(f"sync complete: added {added}, updated {updated}, skipped {skipped}, total {len(s['contacts'])}")

def due_list(cfg, s, group=None):
    gaps = cfg["sequence"]["gaps_days"]; out = []
    filter_group = group.lower() if group else None
    for email, c in s["contacts"].items():
        if c["status"] != "active": continue
        c_seg = (c.get("segment") or "").strip().lower()
        if filter_group and c_seg != filter_group: continue
        step = c["step"]
        if step >= len(gaps): continue
        if step == 0:
            out.append((email, c, 1)); continue
        if c["last_sent_at"]:
            if datetime.now(timezone.utc) - datetime.fromisoformat(c["last_sent_at"]) >= timedelta(days=gaps[step]):
                out.append((email, c, step+1))
    return out

URL_RE = re.compile(r'https?://[^\s<>"]+')

def encode_email(email):
    b = base64.urlsafe_b64encode(email.encode('utf-8')).decode('utf-8')
    return b.rstrip('=')

def build_plain_body(body_text, campaign, recipient_id, tracking_base):
    def replace_url(match):
        url = match.group(0)
        if "/api/c" in url or "/api/o" in url:
            return url
        encoded_url = urllib.parse.quote(url, safe='')
        return f"{tracking_base}/api/c?c={campaign}&r={recipient_id}&u={encoded_url}"
    return URL_RE.sub(replace_url, body_text)

def build_html_body(body_text, campaign, recipient_id, tracking_base):
    escaped_text = html.escape(body_text)
    html_content = escaped_text.replace('\n', '<br>\n')
    
    def replace_url(match):
        url = match.group(0)
        if "/api/c" in url or "/api/o" in url:
            return url
        encoded_url = urllib.parse.quote(url, safe='')
        tracked_url = f"{tracking_base}/api/c?c={campaign}&r={recipient_id}&u={encoded_url}"
        return f'<a href="{tracked_url}" target="_blank" style="color: #1155cc; text-decoration: underline;">{url}</a>'
        
    html_content = URL_RE.sub(replace_url, html_content)
    
    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Malgun Gothic', sans-serif; font-size: 14px; line-height: 1.6; color: #111; margin: 0; padding: 0;">
<div style="padding: 20px; max-width: 600px;">
{html_content}
</div>
<img src="{tracking_base}/api/o?c={campaign}&r={recipient_id}" width="1" height="1" style="display:none !important;" alt="">
</body>
</html>"""
    return html_body

def render(t, name, seg, booking):
    return (t or "").replace("{성명}",name or "").replace("{name}",name or "")\
                     .replace("{유입경로}",seg or "").replace("{booking}",booking or "")

def build_msg(cfg, email, name, seg, step, campaign, recipient_id, tracking_base):
    st = cfg["sequence"]["steps"][step-1]; booking = cfg.get("booking_link","")
    subject = render(st["subject"], name, seg, booking)
    body_text = render(st["body"], name, seg, booking)
    
    if tracking_base:
        body_plain = build_plain_body(body_text, campaign, recipient_id, tracking_base)
        body_html = build_html_body(body_text, campaign, recipient_id, tracking_base)
    else:
        body_plain = body_text
        body_html = None
        
    if cfg.get("footer"):
        footer_text = render(cfg["footer"], name, seg, booking)
        body_plain += "\n\n" + footer_text
        if body_html:
            footer_html = html.escape(footer_text).replace('\n', '<br>\n')
            body_html = body_html.replace('</div>', f'<hr style="border:0;border-top:1px solid #e5e5e5;margin:20px 0;"><div style="font-size:12px;color:#777;">{footer_html}</div></div>')
            
    m = EmailMessage()
    m["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    m["To"] = formataddr((name, email)) if name else email
    if cfg.get("reply_to"): m["Reply-To"] = cfg["reply_to"]
    m["Subject"] = subject
    m["Message-ID"] = make_msgid(domain=cfg["from_email"].split("@")[-1])
    
    if body_html:
        m.set_content(body_plain)
        m.add_alternative(body_html, subtype='html')
    else:
        m.set_content(body_plain)

    attachments = cfg.get("attachments", []) or []
    if isinstance(attachments, str):
        attachments = [attachments]
    for attachment in attachments:
        attachment_path = attachment if os.path.isabs(attachment) else os.path.join(HERE, attachment)
        with open(attachment_path, "rb") as af:
            data = af.read()
        mime_type, _ = mimetypes.guess_type(attachment_path)
        mime_type = mime_type or {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pdf": "application/pdf",
        }.get(os.path.splitext(attachment_path)[1].lower(), "application/octet-stream")
        maintype, subtype = mime_type.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(attachment_path))
    return m, subject

def smtp_connect(cfg):
    host, port = cfg["smtp_host"], int(cfg["smtp_port"])
    mode = str(cfg.get("smtp_security","starttls")).lower()
    if mode == "ssl":
        s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60)
    else:
        s = smtplib.SMTP(host, port, timeout=60)
        if mode == "starttls":
            s.ehlo(); s.starttls(context=ssl.create_default_context()); s.ehlo()
    if cfg.get("smtp_user") and cfg.get("smtp_password"):
        s.login(cfg["smtp_user"], cfg["smtp_password"])
    return s

def _send_one(cfg, msg):
    """메일 1통마다 새 연결로 발송 → 연결 끊김에서 자동 복구. 1회 재시도."""
    last = None
    # 기관 모드에서는 응답 불확실성에 따른 자동 중복 발송을 피한다.
    attempts = (1,) if guard_settings(cfg) is not None else (1, 2)
    for attempt in attempts:
        smtp = None
        try:
            smtp = smtp_connect(cfg)
            smtp.send_message(msg)
            return
        except Exception as e:
            last = e
            time.sleep(5)
        finally:
            if smtp is not None:
                try: smtp.quit()
                except Exception: pass
    raise last

def cmd_run(cfg, dry, group=None):
    s = load_state(cfg); suppress = load_suppress(cfg)
    cap = int(cfg.get("daily_cap",30)); gap = int(cfg.get("min_gap_seconds",45))
    today = _utcnow().date().isoformat()
    sent_today = sum(1 for l in s["log"] if l["ok"] and l["sent_at"][:10] == today)
    remaining = max(0, cap - sent_today)
    due = due_list(cfg, s, group)

    guard = None; blocked = {}
    try:
        gcap = guard_settings(cfg)
        if gcap is not None:
            guard = GlobalGuard(cfg, s, gcap)
    except GuardError as e:
        print(f"전역 안전검사 실패(fail-closed, 발송 안 함): {e}", file=sys.stderr)
        if dry: return
        sys.exit(2)
    if guard:
        elig = []
        for email, c, step in due:
            r = guard.block_reason(email)
            if r is None and email in suppress:
                r = "suppressed"
                if not dry: c["status"] = "optout"
            if r: blocked[r] = blocked.get(r, 0) + 1
            else: elig.append((email, c, step))
        due = elig
        eff = min(remaining, guard.remaining())
        if dry:
            print(f"[GUARD DRY-RUN] eligible={len(due)} would_send={min(len(due), eff)} "
                  f"blocked={json.dumps(blocked, sort_keys=True)} global_remaining={guard.remaining()}/{guard.cap} "
                  f"local_remaining={remaining}/{cap} kst_date={guard.today.isoformat()}")
            return
        remaining_msg = f"{eff}(전역 {guard.remaining()}/{guard.cap}, 로컬 {remaining}/{cap})"
        if blocked: print(f"차단: {json.dumps(blocked, sort_keys=True)}")
        print(f"발송 대상 {len(due)}명 · 오늘 남은 한도 {remaining_msg} · 실제 발송")
        if not due: save_state(cfg, s); return
    
    t_cfg = cfg.get("tracking", {})
    tracking_base = t_cfg.get("base_url", "").rstrip('/')
    campaign_prefix = t_cfg.get("campaign_prefix", "houdini")
    
    if not guard:
        print(f"발송 대상 {len(due)}명 · 오늘 남은 한도 {remaining}/{cap} · {'DRY-RUN' if dry else '실제 발송'}")
        if not due: return
    done = 0; attempted = 0
    for email, c, step in due:
        if not dry and (done >= remaining or (guard and guard.remaining() <= 0)):
            print(f"일일 한도 도달 — 나머지 {len(due)-attempted}명은 다음 실행에."); break
        if email in suppress:
            c["status"] = "optout"; continue
            
        c_seg = (c.get("segment") or "g1").strip().lower()
        campaign = f"{campaign_prefix}-{c_seg}"
        recipient_id = encode_email(email)
        
        msg, subject = build_msg(cfg, email, c["name"], c["segment"], step, campaign, recipient_id, tracking_base)
        if dry:
            print(f"  [{step}통] {c['name'] or ''} <{email}> | {subject}"); done += 1; continue
        attempted += 1
        try:
            _send_one(cfg, msg)
            ts = now()
            c["step"] = step; c["last_sent_at"] = ts
            s["log"].append({"email":email,"step":step,"subject":subject,"sent_at":ts,"ok":True,"error":""})
            if guard: guard.record(email, step, ts)
            print(f"  보냄 [{step}통] {c['name'] or ''} <{email}>"); done += 1
            
            if tracking_base:
                try:
                    sent_url = f"{tracking_base}/api/sent?c={campaign}&r={recipient_id}"
                    req = urllib.request.Request(sent_url, headers={"User-Agent": "varis-sender"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        pass
                except Exception as te:
                    print(f"  [경고] 트래킹 서버 발송 기록 실패: {te}", file=sys.stderr)
                    
            save_state(cfg, s); time.sleep(gap)
        except Exception as e:
            ts = now()
            s["log"].append({"email":email,"step":step,"subject":subject,"sent_at":ts,"ok":False,"error":str(e)})
            if guard:
                c["status"] = "review_required"  # 실패/응답 불확실: 담당자 확인 전 자동 재접촉 금지
                guard.record(email, step, ts); save_state(cfg, s)
            print(f"  실패 <{email}>: {e}", file=sys.stderr)
            time.sleep(gap)
    if not dry: save_state(cfg, s)  # dry-run은 원본을 쓰지 않는다
    print(f"완료: {done}건 {'표시' if dry else '발송'}")

def cmd_status(cfg):
    s = load_state(cfg); by_status = {}; by_step = {}
    for c in s["contacts"].values():
        by_status[c["status"]] = by_status.get(c["status"],0)+1
        if c["status"]=="active": by_step[c["step"]] = by_step.get(c["step"],0)+1
    print("== 파이프라인 ==")
    for k,v in sorted(by_status.items()): print(f"  {k}: {v}")
    print("== 단계별(active) ==")
    for k in sorted(by_step): print(f"  {k}통 완료: {by_step[k]}")
    ok = sum(1 for l in s["log"] if l["ok"]); fail = sum(1 for l in s["log"] if not l["ok"])
    print(f"== 누적 발송 {ok}건 · 실패 {fail}건 ==")

def cmd_mark(cfg, email, status):
    if status not in ("active","replied","optout","done"):
        print("status: active|replied|optout|done", file=sys.stderr); sys.exit(1)
    s = load_state(cfg); email = email.lower()
    if email not in s["contacts"]:
        print(f"{email} 없음", file=sys.stderr); sys.exit(1)
    s["contacts"][email]["status"] = status; save_state(cfg, s)
    print(f"{email} -> {status}")

def cmd_reset(cfg, group):
    s = load_state(cfg); reset_count = 0
    group = group.lower()
    for email, c in s["contacts"].items():
        c_seg = (c.get("segment") or "").strip().lower()
        if c_seg == group:
            c["step"] = 0
            c["status"] = "active"
            c["last_sent_at"] = None
            reset_count += 1
    save_state(cfg, s)
    print(f"리셋 완료: [{group}] 그룹의 {reset_count}명 연락처를 1통차(step 0, active)로 되돌렸습니다.")

def main():
    ap = argparse.ArgumentParser(description="varis.kr 이메일 시퀀스 발송기")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    
    sub.add_parser("sync")
    
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--group", help="특정 그룹만 발송 (예: g2)")
    
    sub.add_parser("status")
    
    m = sub.add_parser("mark")
    m.add_argument("email")
    m.add_argument("status")
    
    rs = sub.add_parser("reset")
    rs.add_argument("--group", required=True, help="리셋할 그룹 (예: g2)")
    
    a = ap.parse_args(); cfg = load_cfg(a.config)
    if a.cmd=="sync": cmd_sync(cfg)
    elif a.cmd=="run": cmd_run(cfg, a.dry_run, a.group)
    elif a.cmd=="status": cmd_status(cfg)
    elif a.cmd=="mark": cmd_mark(cfg, a.email, a.status)
    elif a.cmd=="reset": cmd_reset(cfg, a.group)

if __name__ == "__main__":
    main()
