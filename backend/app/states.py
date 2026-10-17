"""KAZE state machine (pure, DB-free).

`decide(snapshot, event)` returns a `Decision`: the next state plus every side
effect as data (timers, alert changes, messages, timeline entries). The engine
applies a decision inside one database transaction. Nothing here does I/O, reads
a clock or calls a model, so every safety rule below is unit-testable:

* Sensor and heart-rate input arrives only as PULL_FORWARD. It can move ARMED to
  PROMPTED (a counted challenge) and nothing else; it never reaches ESCALATED_*.
* Only a contact's "she's safe" (CONTACT_SAFE) or the wearer's real PIN
  (PIN_REAL / END_REAL) closes an escalation alert as RESOLVED / FALSE_ALARM.
* A device squeeze during an alert leaves the state unchanged.
* A duress squeeze or duress PIN produces exactly the wearer-facing output
  (reply, public state, wearer message, visible timeline entry) of the
  matching OK action; the alert is masked, and from then on the wearer sees a
  twin session that used the real PIN (see "masked duress: the twin").
* LOST_CONTACT has its own wording and never stops pending deadlines.

`_finish` re-checks the first three rules on every decision and raises
InvariantViolation, so a coding mistake fails closed instead of escalating
or standing down.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

# --- states -----------------------------------------------------------------
ARMED = "ARMED"
PROMPTED = "PROMPTED"
REMINDED = "REMINDED"
ACKED = "ACKED"
FINAL = "ESCALATED_FINAL"
ENDED = "ENDED"
RESOLVED = "RESOLVED"
FALSE_ALARM = "FALSE_ALARM"

LINK_OK = "OK"
LINK_LOST = "LOST_CONTACT"

# --- alert kinds / statuses -------------------------------------------------
NO_RESPONSE = "NO_RESPONSE"
DURESS = "DURESS"
LOST_CONTACT = "LOST_CONTACT"

ALERT_OPEN = "OPEN"
ALERT_ACKED = "ACKED"
ALERT_RESTORED = "RESTORED"  # closes a lost-contact notice only, never an escalation

PRE_ALERT = frozenset({ARMED, PROMPTED, REMINDED})
PROMPT_STATES = frozenset({PROMPTED, REMINDED})

CHALLENGE_CHOICES = (1, 3, 4)
ROUTINE_K = 2

# --- events -----------------------------------------------------------------
CHECKIN_DUE = "CHECKIN_DUE"
PULL_FORWARD = "PULL_FORWARD"
SQUEEZE = "SQUEEZE"
WEARER_OK = "WEARER_OK"
PROMPT_TIMEOUT = "PROMPT_TIMEOUT"
REMINDER_TIMEOUT = "REMINDER_TIMEOUT"
ACK_TIMEOUT = "ACK_TIMEOUT"
RENOTIFY = "RENOTIFY"
FOLLOWUP_TIMEOUT = "FOLLOWUP_TIMEOUT"
SESSION_END_DUE = "SESSION_END_DUE"
CONTACT_ACK = "CONTACT_ACK"
CONTACT_PASS = "CONTACT_PASS"
CONTACT_SAFE = "CONTACT_SAFE"
CONTACT_CANT_REACH = "CONTACT_CANT_REACH"
PIN_REAL = "PIN_REAL"
PIN_DURESS = "PIN_DURESS"
END_REAL = "END_REAL"
END_DURESS = "END_DURESS"
LEASE_EXPIRED = "LEASE_EXPIRED"
LOST_CONTACT_NOTIFY = "LOST_CONTACT_NOTIFY"
LINK_RESTORED = "LINK_RESTORED"

# Cover check-ins of a masked duress session (see "Masked duress" below): the
# check-in schedule a twin session that used the real PIN would keep running.
COVER_CHECKIN_DUE = "COVER_CHECKIN_DUE"
COVER_PROMPT_TIMEOUT = "COVER_PROMPT_TIMEOUT"
COVER_REMINDER_TIMEOUT = "COVER_REMINDER_TIMEOUT"
COVER_OF = {CHECKIN_DUE: COVER_CHECKIN_DUE, PROMPT_TIMEOUT: COVER_PROMPT_TIMEOUT,
            REMINDER_TIMEOUT: COVER_REMINDER_TIMEOUT}
TWIN_OF = {v: k for k, v in COVER_OF.items()}
COVER_TIMER_KINDS = tuple(COVER_OF.values())

ALL_EVENTS = (
    CHECKIN_DUE, PULL_FORWARD, SQUEEZE, WEARER_OK, PROMPT_TIMEOUT, REMINDER_TIMEOUT,
    ACK_TIMEOUT, RENOTIFY, FOLLOWUP_TIMEOUT, SESSION_END_DUE, CONTACT_ACK, CONTACT_PASS,
    CONTACT_SAFE, CONTACT_CANT_REACH, PIN_REAL, PIN_DURESS, END_REAL, END_DURESS,
    LEASE_EXPIRED, LOST_CONTACT_NOTIFY, LINK_RESTORED,
) + COVER_TIMER_KINDS
SENSOR_EVENTS = frozenset({PULL_FORWARD})
RESOLVING_EVENTS = frozenset({CONTACT_SAFE, PIN_REAL, END_REAL})
# Wearer-facing events that a masked session answers through its twin.
COVER_EVENTS = frozenset(COVER_TIMER_KINDS) | {SESSION_END_DUE, PULL_FORWARD, SQUEEZE, WEARER_OK,
                                                PIN_REAL, PIN_DURESS, END_REAL, END_DURESS,
                                                LEASE_EXPIRED, LINK_RESTORED}

# Timers tied to the current state carry expected_version and are cancelled on
# every transition. Independent timers survive transitions (cover timers are
# cancelled by cover transitions instead).
STATE_TIMER_KINDS = (CHECKIN_DUE, PROMPT_TIMEOUT, REMINDER_TIMEOUT, ACK_TIMEOUT, RENOTIFY, FOLLOWUP_TIMEOUT)
INDEPENDENT_TIMER_KINDS = (SESSION_END_DUE, LOST_CONTACT_NOTIFY) + COVER_TIMER_KINDS
TIMER_KINDS = STATE_TIMER_KINDS + INDEPENDENT_TIMER_KINDS

SQUEEZE_PATTERNS = ("plain", "duress", "unrecognized")

FINAL_BANNER = (
    "Emergency services: SIMULATED – would contact 911 / campus safety with last known location"
)


def level_state(i: int) -> str:
    return f"ESCALATED_L{i}"


def is_escalated(state: str) -> bool:
    return state.startswith("ESCALATED_")


def is_alerting(state: str) -> bool:
    return is_escalated(state) or state == ACKED


class InvariantViolation(RuntimeError):
    """A decision broke a KAZE safety rule. Raised instead of applying it."""


# --- inputs -----------------------------------------------------------------
@dataclass(frozen=True)
class Timings:
    name: str
    interval_s: float
    prompt_window_s: float
    delivery_grace_s: float
    ack_s: float
    duress_ack_s: float
    renotify_s: float
    renotify_cap: int
    followup_s: float
    link_expiry_s: float
    heartbeat_s: float
    lease_s: float
    lost_contact_grace_s: float
    hr_sustain_s: float
    hr_eval_s: float
    sensor_cooldown_s: float
    hr_lost_s: float


@dataclass(frozen=True)
class AlertView:
    kind: str
    level: int
    is_final: bool = False
    masked: bool = False
    status: str = ALERT_OPEN
    acked_by: Optional[int] = None
    renotify_count: int = 0
    recipients: tuple[int, ...] = ()
    id: Optional[int] = None


@dataclass(frozen=True)
class Snapshot:
    state: str
    public_state: str
    version: int
    timings: Timings
    contacts: tuple[int, ...] = ()  # approved, not revoked, escalation order
    alert: Optional[AlertView] = None  # the open alert, if any
    challenge_n: Optional[int] = None
    prompt_cause: Optional[str] = None
    retry_used: bool = False
    link_state: str = LINK_OK
    lost_contact_told: bool = False
    window_multiplier: float = 1.0


@dataclass(frozen=True)
class Event:
    kind: str
    pattern: Optional[str] = None  # SQUEEZE: plain | duress | unrecognized
    k: Optional[int] = None  # SQUEEZE: squeeze count
    contact_id: Optional[int] = None  # CONTACT_*
    reason: Optional[str] = None  # PULL_FORWARD
    level: Optional[str] = None  # PULL_FORWARD: LOW | MED | HIGH
    late: bool = False  # SQUEEZE delivered after a gap
    simulated: bool = False  # PULL_FORWARD from simulated input (badged)


# --- outputs ----------------------------------------------------------------
@dataclass(frozen=True)
class TimerSpec:
    kind: str
    delay_s: float
    prompt_window_s: Optional[float] = None  # set for prompt deadlines (lowered on delivery)


@dataclass(frozen=True)
class AlertOp:
    op: str  # open | level | ack | close | upgrade | renotify
    kind: Optional[str] = None
    level: Optional[int] = None
    is_final: bool = False
    masked: Optional[bool] = None
    contact_id: Optional[int] = None
    status: Optional[str] = None
    by: Optional[str] = None


@dataclass(frozen=True)
class Notify:
    to: str  # wearer | contact
    template: str
    contact_id: Optional[int] = None
    params: dict = field(default_factory=dict)
    buttons: Optional[str] = None  # alert | followup
    deadline_timer: Optional[str] = None  # links a wearer prompt to its deadline timer


@dataclass(frozen=True)
class AuditEntry:
    kind: str
    detail: dict = field(default_factory=dict)
    masked: Optional[bool] = None  # None: masked iff a masked alert is open after the decision
    from_state: Optional[str] = None  # overrides (used for the visible cover entry)
    to_state: Optional[str] = None


@dataclass
class Decision:
    event: Event
    from_state: str
    to_state: str
    public_state: str
    changed: bool = False
    challenge_n: Optional[int] = None
    prompt_cause: Optional[str] = None
    retry_used: bool = False
    link_state: str = LINK_OK
    lost_contact_told: bool = False
    stamp: Optional[str] = None  # prompted_at | reminded_at
    stamp_lost: Optional[bool] = None  # True: set lost_contact_at; False: clear it
    cancel_state_timers: bool = False
    cancel_all_timers: bool = False
    cancel_kinds: list[str] = field(default_factory=list)
    timers: list[TimerSpec] = field(default_factory=list)
    alert_ops: list[AlertOp] = field(default_factory=list)
    notify: list[Notify] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    location: Optional[str] = None  # release | expire_now | expire_later
    reply: str = "ignored"
    through: Optional[str] = None  # RESOLVED | FALSE_ALARM when the session passes through it
    alert_open_after: bool = False
    alert_masked_after: bool = False
    alert_kind_after: Optional[str] = None
    cover: bool = False  # the wearer-facing output came from the twin (masked duress)
    rekind_cover: bool = False  # the mask opens: pending check-in timers become cover timers
    public_changed: bool = False  # the public prompt changed (the device gets a new challenge id)

    @property
    def wearer_templates(self) -> list[str]:
        return [n.template for n in self.notify if n.to == "wearer"]


Chooser = Callable[[Sequence[int]], int]


def _secure_choice(options: Sequence[int]) -> int:
    return secrets.choice(list(options))


# --- helpers ----------------------------------------------------------------
def _base(snap: Snapshot, ev: Event) -> Decision:
    return Decision(
        event=ev,
        from_state=snap.state,
        to_state=snap.state,
        public_state=snap.public_state,
        challenge_n=snap.challenge_n,
        prompt_cause=snap.prompt_cause,
        retry_used=snap.retry_used,
        link_state=snap.link_state,
        lost_contact_told=snap.lost_contact_told,
    )


def _window(snap: Snapshot) -> float:
    return snap.timings.prompt_window_s * max(1.0, min(10.0, snap.window_multiplier))


def _recipients(snap: Snapshot) -> list[int]:
    """Contacts already told about the open alert, still approved."""
    if snap.alert is None:
        return []
    approved = set(snap.contacts)
    return [c for c in snap.alert.recipients if c in approved]


def _alert_template(kind: str, final: bool) -> str:
    if final:
        return "alert_final"
    if kind == DURESS:
        return "alert_duress"
    return "alert_no_response"


def _enter_prompt(snap: Snapshot, d: Decision, to: str, cause: str, n: int, template: str) -> None:
    w = _window(snap)
    d.to_state = to
    d.changed = True
    d.cancel_state_timers = True
    d.challenge_n = n
    d.prompt_cause = cause
    d.retry_used = False
    d.stamp = "prompted_at" if to == PROMPTED else "reminded_at"
    kind = PROMPT_TIMEOUT if to == PROMPTED else REMINDER_TIMEOUT
    # Prompt deadlines start at delivery: now + window + grace, lowered to
    # delivered_at + window once the nudge is sent or the device shows it.
    d.timers.append(TimerSpec(kind, w + snap.timings.delivery_grace_s, prompt_window_s=w))
    params = {"n": n} if template == "reminder_n" else {}
    d.notify.append(Notify("wearer", template, params=params, deadline_timer=kind))
    d.reply = "prompted"


def _back_to_armed(snap: Snapshot, d: Decision, schedule: bool = True) -> None:
    d.to_state = ARMED
    d.changed = True
    d.cancel_state_timers = True
    d.challenge_n = None
    d.prompt_cause = None
    d.retry_used = False
    if schedule:
        d.timers.append(TimerSpec(CHECKIN_DUE, snap.timings.interval_s))


def _go_level(snap: Snapshot, d: Decision, level: int, kind: str) -> None:
    """Move the escalation ladder to `level` (or FINAL past the last contact)."""
    t = snap.timings
    contacts = list(snap.contacts)
    if level > len(contacts):
        _enter_final(snap, d, len(contacts) + 1)
        return
    d.to_state = level_state(level)
    d.changed = True
    d.cancel_state_timers = True
    d.challenge_n = None
    d.prompt_cause = None
    d.retry_used = False
    d.alert_ops.append(AlertOp("level", level=level, is_final=False))
    template = _alert_template(kind, False)
    target = contacts[level - 1]
    d.notify.append(Notify("contact", template, contact_id=target, buttons="alert"))
    for earlier in contacts[: level - 1]:  # re-ping earlier contacts
        d.notify.append(Notify("contact", template, contact_id=earlier, buttons="alert"))
    ack = t.duress_ack_s if (kind == DURESS and level == 1) else t.ack_s
    d.timers.append(TimerSpec(ACK_TIMEOUT, ack))
    d.timers.append(TimerSpec(RENOTIFY, t.renotify_s))
    d.audit.append(AuditEntry("escalated", {"level": level, "alert_kind": kind, "contact_id": target}))


def _enter_final(snap: Snapshot, d: Decision, level: int) -> None:
    d.to_state = FINAL
    d.changed = True
    d.cancel_state_timers = True
    d.challenge_n = None
    d.prompt_cause = None
    d.retry_used = False
    d.alert_ops.append(AlertOp("level", level=level, is_final=True))
    for c in snap.contacts:
        d.notify.append(Notify("contact", "alert_final", contact_id=c, buttons="alert"))
    d.timers.append(TimerSpec(RENOTIFY, snap.timings.renotify_s))
    # Simulated only: logged, never placed. There is no code path that calls anyone.
    d.audit.append(AuditEntry("emergency_services_simulated", {"placed": False, "banner": FINAL_BANNER}))


def _open_escalation(snap: Snapshot, d: Decision, kind: str, masked: bool) -> None:
    """Open (or upgrade a lost-contact notice into) an escalation alert at L1."""
    if snap.alert is not None and snap.alert.kind == LOST_CONTACT:
        d.alert_ops.append(AlertOp("upgrade", kind=kind, masked=masked))
    else:
        d.alert_ops.append(AlertOp("open", kind=kind, level=1, masked=masked))
    d.location = "release"
    _go_level(snap, d, 1, kind)


# Visible timeline entries of the OK actions. Duress paths emit the same entries
# (as cover) so the wearer's timeline is indistinguishable while the alert is open.
def _ok_checkin_entry(snap: Snapshot, via: str, to_public: str) -> AuditEntry:
    return AuditEntry("checkin", {"via": via, "result": "noted"}, masked=False,
                      from_state=snap.public_state, to_state=to_public)


def _pin_entry(snap: Snapshot, action: str, to_public: str) -> AuditEntry:
    return AuditEntry(action, {"via": "pin"}, masked=False,
                      from_state=snap.public_state, to_state=to_public)


def _late_entry(via: str, late: bool) -> AuditEntry:
    return AuditEntry("late_response", {"via": via, "late": bool(late)})


def _wearer_ack_template(snap: Snapshot) -> str:
    """What the wearer sees after a squeeze. Depends on public state only."""
    return "noted_alerting" if is_alerting(snap.public_state) else "noted"


# --- event handlers ---------------------------------------------------------
def _on_pull_forward(snap: Snapshot, ev: Event, d: Decision, choose: Chooser) -> None:
    detail = {"reason": ev.reason, "level": ev.level, "simulated": bool(ev.simulated)}
    if snap.state == ARMED:
        n = choose(CHALLENGE_CHOICES)
        _enter_prompt(snap, d, PROMPTED, "challenge", n, "reminder_n")
        d.audit.append(AuditEntry("pull_forward", {**detail, "n": n}))
    else:
        d.audit.append(AuditEntry("pull_forward_skipped", detail))
        d.reply = "ignored"


def _on_checkin_due(snap: Snapshot, d: Decision) -> None:
    if snap.state != ARMED:
        d.reply = "ignored"
        return
    _enter_prompt(snap, d, PROMPTED, "routine", ROUTINE_K, "reminder")
    d.audit.append(AuditEntry("checkin_due", {}))
    # Real profile: contacts hear about a lost link once a check-in comes due.
    if snap.link_state == LINK_LOST and snap.alert is None and snap.contacts and not snap.lost_contact_told:
        d.alert_ops.append(AlertOp("open", kind=LOST_CONTACT, level=1, masked=False))
        d.notify.append(Notify("contact", "lost_contact", contact_id=snap.contacts[0]))
        d.lost_contact_told = True
        d.cancel_kinds.append(LOST_CONTACT_NOTIFY)


def _on_session_end_due(snap: Snapshot, d: Decision) -> None:
    if snap.state == ARMED:
        _enter_prompt(snap, d, PROMPTED, "session_end", ROUTINE_K, "home_safe")
        d.audit.append(AuditEntry("session_end_due", {}))
    elif snap.state in PROMPT_STATES or is_alerting(snap.state):
        # Not now: ask "Home safe?" once the current prompt or alert is over.
        d.timers.append(TimerSpec(SESSION_END_DUE, _window(snap) + snap.timings.delivery_grace_s))
        d.reply = "deferred"
    else:
        d.reply = "ignored"


def _on_prompt_timeout(snap: Snapshot, d: Decision, choose: Chooser) -> None:
    if snap.state != PROMPTED:
        d.reply = "ignored"
        return
    n = choose(CHALLENGE_CHOICES)
    _enter_prompt(snap, d, REMINDED, "challenge", n, "reminder_n")
    d.audit.append(AuditEntry("prompt_timeout", {"n": n}))


def _on_reminder_timeout(snap: Snapshot, d: Decision) -> None:
    if snap.state != REMINDED:
        d.reply = "ignored"
        return
    d.audit.append(AuditEntry("reminder_timeout", {}))
    _open_escalation(snap, d, NO_RESPONSE, masked=False)
    d.reply = "escalated"


def _mismatch(snap: Snapshot, ev: Event, d: Decision, choose: Chooser) -> None:
    d.audit.append(AuditEntry("checkin_mismatch", {
        "got": ev.k, "pattern": ev.pattern or "plain", "expected": snap.challenge_n}))
    d.reply = "retry"
    if not snap.retry_used:
        # One retry with a new N; the deadline is unchanged (no version bump).
        options = [c for c in CHALLENGE_CHOICES if c != snap.challenge_n] or list(CHALLENGE_CHOICES)
        n = choose(options)
        d.challenge_n = n
        d.prompt_cause = "challenge"
        d.retry_used = True
        d.notify.append(Notify("wearer", "reminder_n", params={"n": n}))


def _late_response(snap: Snapshot, d: Decision, via: str, late: bool) -> None:
    """A wearer response during an alert. Never stands the alert down."""
    template = "late_device" if via == "device" else "late_phone"
    for c in _recipients(snap):
        d.notify.append(Notify("contact", template, contact_id=c, params={"late": bool(late)}))
    d.notify.append(Notify("wearer", _wearer_ack_template(snap)))
    d.audit.append(_late_entry(via, late))
    d.reply = "noted"


def _on_squeeze(snap: Snapshot, ev: Event, d: Decision, choose: Chooser) -> None:
    pattern = ev.pattern or "plain"
    s = snap.state
    if pattern == "duress":
        _duress_squeeze(snap, ev, d)
        return
    if s in PROMPT_STATES:
        if pattern == "plain" and ev.k is not None and ev.k == snap.challenge_n:
            _back_to_armed(snap, d)
            d.notify.append(Notify("wearer", "noted"))
            d.audit.append(_ok_checkin_entry(snap, "device", ARMED))
            d.reply = "noted"
        else:
            _mismatch(snap, ev, d, choose)
    elif s == ARMED:
        if pattern == "plain":
            # Unprompted squeeze: acknowledged, schedule untouched (stray squeezes
            # must not push the next check-in back).
            d.notify.append(Notify("wearer", "noted"))
            d.audit.append(_ok_checkin_entry(snap, "device", ARMED))
            d.reply = "noted"
        else:
            d.reply = "ignored"
    elif is_alerting(s):
        _late_response(snap, d, "device", ev.late)


def _duress_squeeze(snap: Snapshot, ev: Event, d: Decision) -> None:
    s = snap.state
    if s in PRE_ALERT:
        # The wearer-facing output (reply, public state, "Noted ✓", visible entry, check-in
        # schedule) comes from the twin, a correct plain squeeze: see _cover().
        d.audit.append(AuditEntry("duress", {"via": "device"}, masked=True))
        _open_escalation(snap, d, DURESS, masked=True)
    elif is_alerting(s):
        # Same wearer output as a late plain squeeze; contacts get the help request.
        if snap.alert is not None and snap.alert.kind != DURESS:
            d.alert_ops.append(AlertOp("upgrade", kind=DURESS))
        for c in _recipients(snap):
            d.notify.append(Notify("contact", "alert_duress", contact_id=c, buttons="alert"))
        d.notify.append(Notify("wearer", _wearer_ack_template(snap)))
        d.audit.append(_late_entry("device", ev.late))
        d.audit.append(AuditEntry("duress", {"via": "device"}, masked=True))
        d.reply = "noted"


def _on_wearer_ok(snap: Snapshot, d: Decision) -> None:
    s = snap.state
    if s in PROMPT_STATES or s == ARMED:
        _back_to_armed(snap, d)
        d.notify.append(Notify("wearer", "noted"))
        d.audit.append(_ok_checkin_entry(snap, "phone", ARMED))
        d.reply = "noted"
    elif is_alerting(s):
        _late_response(snap, d, "phone", False)


def _false_alarm_entry(snap: Snapshot, to_public: str) -> AuditEntry:
    # "through" is part of the entry itself, so the duress cover entry carries it too.
    return AuditEntry("false_alarm", {"by": "wearer_pin", "through": FALSE_ALARM}, masked=False,
                      from_state=snap.public_state, to_state=to_public)


def _false_alarm(snap: Snapshot, d: Decision, to: str) -> None:
    d.through = FALSE_ALARM
    d.alert_ops.append(AlertOp("close", status=FALSE_ALARM, by="wearer_pin"))
    for c in _recipients(snap):
        d.notify.append(Notify("contact", "false_alarm", contact_id=c))
    d.location = "expire_now"
    if to == ENDED:
        _to_ended(d)
    else:
        _back_to_armed(snap, d)
    d.audit.append(_false_alarm_entry(snap, to))


def _to_ended(d: Decision) -> None:
    d.to_state = ENDED
    d.changed = True
    d.cancel_all_timers = True
    d.challenge_n = None
    d.prompt_cause = None
    d.retry_used = False


def _on_pin_real(snap: Snapshot, d: Decision, end: bool) -> None:
    s = snap.state
    action, reply, wearer_t = ("end", "ended", "ended") if end else ("cancel", "cancelled", "cancelled")
    target_public = ENDED if end else ARMED
    if s in PRE_ALERT:
        d.audit.append(_pin_entry(snap, action, target_public))
        if snap.alert is not None and snap.alert.kind == LOST_CONTACT:
            _false_alarm(snap, d, target_public)
        elif end:
            _to_ended(d)
        elif s in PROMPT_STATES:
            _back_to_armed(snap, d)
    elif is_alerting(s):
        if snap.alert is not None and snap.alert.kind == DURESS:
            # A real PIN never closes a duress alert; only "she's safe" does. While it is
            # masked, what the wearer sees comes from the twin (see _cover).
            for c in _recipients(snap):
                d.notify.append(Notify("contact", "duress_pin_note", contact_id=c))
            d.audit.append(AuditEntry("real_pin_on_duress", {"action": action}, masked=True))
        else:
            d.audit.append(_pin_entry(snap, action, target_public))
            _false_alarm(snap, d, target_public)
    else:
        return
    d.notify.append(Notify("wearer", wearer_t))
    d.reply = reply


def _on_pin_duress(snap: Snapshot, d: Decision, end: bool) -> None:
    s = snap.state
    action, reply, wearer_t = ("end", "ended", "ended") if end else ("cancel", "cancelled", "cancelled")
    if s in PRE_ALERT:
        # Wearer-facing output comes from the twin (the real PIN): see _cover().
        d.audit.append(AuditEntry("duress", {"via": "pin", "action": action}, masked=True))
        _open_escalation(snap, d, DURESS, masked=True)
    elif is_alerting(s):
        alert = snap.alert
        if alert is not None and alert.kind == DURESS:
            for c in _recipients(snap):
                d.notify.append(Notify("contact", "alert_duress", contact_id=c, buttons="alert"))
            d.audit.append(AuditEntry("duress", {"via": "pin", "action": action}, masked=True))
        else:
            # The real PIN would close this alert as a false alarm. Mask instead: escalation
            # continues underneath and the twin (the false alarm) is what the wearer sees.
            d.audit.append(AuditEntry("duress", {"via": "pin", "action": action}, masked=True))
            d.alert_ops.append(AlertOp("upgrade", kind=DURESS, masked=True))
            for c in _recipients(snap):
                d.notify.append(Notify("contact", "alert_duress", contact_id=c, buttons="alert"))
    else:
        return
    d.notify.append(Notify("wearer", wearer_t))
    d.reply = reply


def _on_ack_timeout(snap: Snapshot, d: Decision) -> None:
    if is_escalated(snap.state) and snap.state != FINAL and snap.alert is not None:
        d.audit.append(AuditEntry("ack_timeout", {"level": snap.alert.level}))
        _go_level(snap, d, snap.alert.level + 1, snap.alert.kind)
        d.reply = "escalated"
    else:
        d.reply = "ignored"


def _on_renotify(snap: Snapshot, d: Decision) -> None:
    alert = snap.alert
    if not (is_escalated(snap.state) and alert is not None):
        d.reply = "ignored"
        return
    t = snap.timings
    if alert.renotify_count >= t.renotify_cap:
        d.reply = "capped"
        return
    if snap.state == FINAL:
        targets = list(snap.contacts)
    else:
        lvl = alert.level
        targets = [snap.contacts[lvl - 1]] if 0 < lvl <= len(snap.contacts) else []
    template = _alert_template(alert.kind, snap.state == FINAL)
    for c in targets:
        d.notify.append(Notify("contact", template, contact_id=c, buttons="alert"))
    d.alert_ops.append(AlertOp("renotify"))
    if alert.renotify_count + 1 < t.renotify_cap:
        d.timers.append(TimerSpec(RENOTIFY, t.renotify_s))
    d.audit.append(AuditEntry("renotify", {"count": alert.renotify_count + 1}))
    d.reply = "renotified"


def _on_contact_ack(snap: Snapshot, ev: Event, d: Decision) -> None:
    alert = snap.alert
    c = ev.contact_id
    if is_escalated(snap.state) and alert is not None and c in _recipients(snap):
        d.to_state = ACKED
        d.changed = True
        d.cancel_state_timers = True
        d.alert_ops.append(AlertOp("ack", contact_id=c))
        for r in _recipients(snap):
            if r != c:
                d.notify.append(Notify("contact", "handling", contact_id=r, params={"contact_id": c}))
        d.notify.append(Notify("contact", "ack_thanks", contact_id=c, buttons="followup"))
        d.timers.append(TimerSpec(FOLLOWUP_TIMEOUT, snap.timings.followup_s))
        d.audit.append(AuditEntry("acked", {"contact_id": c}))
        d.reply = "acked"
    elif snap.state == ACKED:
        d.reply = "already_handled"
    else:
        d.reply = "ignored"


def _on_contact_pass(snap: Snapshot, ev: Event, d: Decision) -> None:
    alert = snap.alert
    if (is_escalated(snap.state) and snap.state != FINAL and alert is not None
            and 0 < alert.level <= len(snap.contacts) and snap.contacts[alert.level - 1] == ev.contact_id):
        d.audit.append(AuditEntry("passed", {"contact_id": ev.contact_id, "level": alert.level}))
        _go_level(snap, d, alert.level + 1, alert.kind)
        d.reply = "passed"
    else:
        d.reply = "ignored"


def _on_contact_safe(snap: Snapshot, ev: Event, d: Decision) -> None:
    alert = snap.alert
    if not (snap.state == ACKED and alert is not None and ev.contact_id is not None
            and ev.contact_id == alert.acked_by):
        # An acknowledgement alone never closes an alert, and only the contact
        # who took it on can confirm she's safe.
        d.reply = "ignored"
        return
    target = ENDED if snap.public_state == ENDED else ARMED
    d.through = RESOLVED
    d.cancel_kinds += list(COVER_TIMER_KINDS)  # a masked alert's cover schedule ends with it
    d.alert_ops.append(AlertOp("close", status=RESOLVED, by=f"contact:{ev.contact_id}"))
    for c in _recipients(snap):
        d.notify.append(Notify("contact", "resolved", contact_id=c, params={"contact_id": ev.contact_id}))
    d.location = "expire_later"
    if target == ENDED:
        _to_ended(d)
    else:
        _back_to_armed(snap, d)
    d.public_state = target
    d.audit.append(AuditEntry("resolved", {"by": f"contact:{ev.contact_id}"}, masked=False))
    d.reply = "resolved"


def _on_followup_missed(snap: Snapshot, ev: Event, d: Decision) -> None:
    alert = snap.alert
    if not (snap.state == ACKED and alert is not None):
        d.reply = "ignored"
        return
    if ev.kind == CONTACT_CANT_REACH and ev.contact_id != alert.acked_by:
        d.reply = "ignored"
        return
    acker = alert.acked_by
    d.audit.append(AuditEntry("followup_missed" if ev.kind == FOLLOWUP_TIMEOUT else "cant_reach",
                              {"contact_id": acker}))
    if alert.is_final:
        _enter_final(snap, d, alert.level)
    else:
        _go_level(snap, d, alert.level + 1, alert.kind)
    # Re-ping the contact who acknowledged.
    if acker is not None and acker in snap.contacts and not any(
            n.to == "contact" and n.contact_id == acker for n in d.notify):
        tmpl = _alert_template(alert.kind, d.to_state == FINAL)
        d.notify.append(Notify("contact", tmpl, contact_id=acker, buttons="alert"))
    d.reply = "escalated"


def _on_lease_expired(snap: Snapshot, d: Decision) -> None:
    if snap.link_state == LINK_LOST or snap.state == ENDED:
        d.reply = "ignored"
        return
    d.link_state = LINK_LOST
    d.stamp_lost = True
    d.notify.append(Notify("wearer", "wearer_lost_contact"))
    rec = _recipients(snap)
    if snap.alert is not None and rec:
        for c in rec:
            d.notify.append(Notify("contact", "lost_contact", contact_id=c))
        d.lost_contact_told = True
    else:
        d.lost_contact_told = False
        d.timers.append(TimerSpec(LOST_CONTACT_NOTIFY, snap.timings.lost_contact_grace_s))
    d.audit.append(AuditEntry("lost_contact", {}))
    d.reply = "lost_contact"


def _on_lost_contact_notify(snap: Snapshot, d: Decision) -> None:
    if snap.link_state != LINK_LOST or snap.lost_contact_told:
        d.reply = "ignored"
        return
    if snap.alert is not None:
        rec = _recipients(snap)
        if not rec:
            d.reply = "ignored"
            return
        for c in rec:
            d.notify.append(Notify("contact", "lost_contact", contact_id=c))
        d.lost_contact_told = True
    elif snap.contacts:
        d.alert_ops.append(AlertOp("open", kind=LOST_CONTACT, level=1, masked=False))
        d.notify.append(Notify("contact", "lost_contact", contact_id=snap.contacts[0]))
        d.lost_contact_told = True
    else:
        d.reply = "ignored"
        return
    d.audit.append(AuditEntry("lost_contact_shared", {}))
    d.reply = "lost_contact_shared"


def _on_link_restored(snap: Snapshot, d: Decision) -> None:
    if snap.link_state != LINK_LOST:
        d.reply = "ignored"
        return
    d.link_state = LINK_OK
    d.stamp_lost = False
    d.cancel_kinds.append(LOST_CONTACT_NOTIFY)
    d.notify.append(Notify("wearer", "wearer_restored"))
    if snap.lost_contact_told:
        for c in _recipients(snap):
            d.notify.append(Notify("contact", "restored", contact_id=c))
    if snap.alert is not None and snap.alert.kind == LOST_CONTACT:
        # Closes the link notice only. Escalation alerts stay open.
        d.alert_ops.append(AlertOp("close", status=ALERT_RESTORED, by="link"))
    d.lost_contact_told = False
    d.audit.append(AuditEntry("link_restored", {}))
    d.reply = "restored"


# --- masked duress: the twin ----------------------------------------------------
# While a masked duress alert is open, the escalation runs on `state` and the wearer
# sees a cover: `public_state` (and the session's challenge fields) follow a *twin*,
# the session she would have if she had used the real PIN or a plain squeeze. Every
# wearer-facing event (COVER_EVENTS) is decided twice: the real decision keeps its
# contact messages, alert changes and hidden entries; the twin's decision supplies
# the reply, public state, prompt, wearer messages, visible entries and the cover
# check-in timers. Nothing the twin does can touch the alert: its alert operations
# and contact messages are dropped, so only "she's safe" closes a duress alert.
def is_masked(snap: Snapshot) -> bool:
    return snap.alert is not None and snap.alert.masked and is_alerting(snap.state)


def _twin_snapshot(snap: Snapshot) -> Snapshot:
    if is_alerting(snap.public_state):
        # The cover itself escalated (its own check-in went unanswered): a no-response twin.
        return replace(snap, state=snap.public_state, alert=replace(snap.alert, kind=NO_RESPONSE, masked=False))
    return replace(snap, state=snap.public_state, alert=None)


def _ok_event(twin: Snapshot, ev: Event) -> Event:
    """The event the twin sees: the OK action for a duress one, the plain timer for a cover timer."""
    k = ev.kind
    if k in TWIN_OF:
        return replace(ev, kind=TWIN_OF[k])
    if k == PIN_DURESS:
        return replace(ev, kind=PIN_REAL)
    if k == END_DURESS:
        return replace(ev, kind=END_REAL)
    if k == SQUEEZE and ev.pattern == "duress":
        n = twin.challenge_n if twin.state in PROMPT_STATES else ROUTINE_K
        return replace(ev, pattern="plain", k=n)
    return ev


def _cover(snap: Snapshot, d: Decision, twin: Decision, opening: bool) -> None:
    d.cover = True
    d.rekind_cover = opening
    escalates = is_alerting(twin.to_state) and not is_alerting(twin.from_state)
    # A twin whose check-in went unanswered escalates: from then on the cover follows the ladder.
    d.public_state = d.to_state if escalates else twin.to_state
    d.challenge_n, d.prompt_cause, d.retry_used = twin.challenge_n, twin.prompt_cause, twin.retry_used
    d.stamp = twin.stamp
    d.public_changed = twin.changed
    d.reply = twin.reply
    d.notify = [n for n in d.notify if n.to != "wearer"] + [
        replace(n, deadline_timer=COVER_OF.get(n.deadline_timer, n.deadline_timer))
        for n in twin.notify if n.to == "wearer"]
    visible = [AuditEntry(e.kind, e.detail, False, e.from_state or twin.from_state, e.to_state or twin.to_state)
               for e in twin.audit if not e.masked]
    d.audit = visible + [replace(e, masked=True) for e in d.audit if e.masked is not False]
    d.timers = [t for t in d.timers if t.kind != SESSION_END_DUE]
    for t in twin.timers:
        if t.kind in COVER_OF:
            d.timers.append(replace(t, kind=COVER_OF[t.kind]))
        elif t.kind == SESSION_END_DUE:
            d.timers.append(t)
    if twin.cancel_state_timers or twin.cancel_all_timers:
        d.cancel_kinds += list(COVER_TIMER_KINDS)
    if twin.cancel_all_timers:
        d.cancel_kinds.append(SESSION_END_DUE)


# --- entry point ------------------------------------------------------------
def decide(snap: Snapshot, ev: Event, choose: Optional[Chooser] = None) -> Decision:
    choose = choose or _secure_choice
    d = _base(snap, ev)
    if snap.state == ENDED:
        return _finish(snap, d)
    _dispatch(snap, ev, d, choose)
    open_after, masked_after, _ = _alert_after(snap, d)
    if open_after and masked_after:
        if not is_masked(snap):
            # This decision opens the mask: the twin is the same session taking the OK action.
            _cover(snap, d, decide(snap, _ok_event(snap, ev), choose), opening=True)
        elif ev.kind in COVER_EVENTS:
            twin = _twin_snapshot(snap)
            _cover(snap, d, decide(twin, _ok_event(twin, ev), choose), opening=False)
    return _finish(snap, d)


def _dispatch(snap: Snapshot, ev: Event, d: Decision, choose: Chooser) -> None:
    k = ev.kind
    if k == PULL_FORWARD:
        _on_pull_forward(snap, ev, d, choose)
    elif k == CHECKIN_DUE:
        _on_checkin_due(snap, d)
    elif k == SESSION_END_DUE:
        _on_session_end_due(snap, d)
    elif k == PROMPT_TIMEOUT:
        _on_prompt_timeout(snap, d, choose)
    elif k == REMINDER_TIMEOUT:
        _on_reminder_timeout(snap, d)
    elif k == SQUEEZE:
        _on_squeeze(snap, ev, d, choose)
    elif k == WEARER_OK:
        _on_wearer_ok(snap, d)
    elif k == PIN_REAL:
        _on_pin_real(snap, d, end=False)
    elif k == END_REAL:
        _on_pin_real(snap, d, end=True)
    elif k == PIN_DURESS:
        _on_pin_duress(snap, d, end=False)
    elif k == END_DURESS:
        _on_pin_duress(snap, d, end=True)
    elif k == ACK_TIMEOUT:
        _on_ack_timeout(snap, d)
    elif k == RENOTIFY:
        _on_renotify(snap, d)
    elif k == CONTACT_ACK:
        _on_contact_ack(snap, ev, d)
    elif k == CONTACT_PASS:
        _on_contact_pass(snap, ev, d)
    elif k == CONTACT_SAFE:
        _on_contact_safe(snap, ev, d)
    elif k in (CONTACT_CANT_REACH, FOLLOWUP_TIMEOUT):
        _on_followup_missed(snap, ev, d)
    elif k == LEASE_EXPIRED:
        _on_lease_expired(snap, d)
    elif k == LOST_CONTACT_NOTIFY:
        _on_lost_contact_notify(snap, d)
    elif k == LINK_RESTORED:
        _on_link_restored(snap, d)
    elif k in COVER_TIMER_KINDS:
        d.reply = "ignored"  # the twin answers these while a masked alert is open
    else:
        raise ValueError(f"unknown event {k!r}")


def _alert_after(snap: Snapshot, d: Decision) -> tuple[bool, bool, Optional[str]]:
    open_ = snap.alert is not None
    masked = snap.alert.masked if snap.alert else False
    kind = snap.alert.kind if snap.alert else None
    for op in d.alert_ops:
        if op.op == "open":
            open_, masked, kind = True, bool(op.masked), op.kind
        elif op.op == "upgrade":
            kind = op.kind or kind
            if op.masked is not None:
                masked = op.masked
        elif op.op == "close":
            open_ = False
    return open_, masked, kind


def _finish(snap: Snapshot, d: Decision) -> Decision:
    open_after, masked_after, kind_after = _alert_after(snap, d)
    d.alert_open_after, d.alert_masked_after, d.alert_kind_after = open_after, masked_after, kind_after
    hide = open_after and masked_after
    if not hide:
        d.public_state = d.to_state
        d.public_changed = d.changed
    elif not d.cover:
        if is_alerting(snap.public_state):
            # The cover escalated earlier: it follows the ladder like the twin would.
            d.public_state = d.to_state
            d.public_changed = d.changed
        else:
            # An event the wearer cannot see: her cover prompt stays exactly as it was.
            d.challenge_n, d.prompt_cause, d.retry_used = snap.challenge_n, snap.prompt_cause, snap.retry_used
            d.public_changed = False
    d.audit = [
        e if e.masked is not None else AuditEntry(e.kind, e.detail, hide, e.from_state, e.to_state)
        for e in d.audit
    ]
    _check_invariants(snap, d)
    return d


def _check_invariants(snap: Snapshot, d: Decision) -> None:
    ev = d.event
    closes = [op for op in d.alert_ops if op.op == "close"]
    if ev.kind in SENSOR_EVENTS:
        if d.to_state not in (snap.state, PROMPTED):
            raise InvariantViolation(f"sensor event moved {snap.state} -> {d.to_state}")
        if is_alerting(d.to_state) and not is_alerting(snap.state):
            raise InvariantViolation("sensor event reached an escalation")
        if d.alert_ops:
            raise InvariantViolation("sensor event touched an alert")
    for op in closes:
        if op.status in (RESOLVED, FALSE_ALARM) and ev.kind not in RESOLVING_EVENTS:
            raise InvariantViolation(f"{ev.kind} tried to resolve an alert")
        if op.status == ALERT_RESTORED and (snap.alert is None or snap.alert.kind != LOST_CONTACT):
            raise InvariantViolation("link restore tried to close an escalation alert")
    if is_alerting(snap.state) and not is_alerting(d.to_state) and ev.kind not in RESOLVING_EVENTS:
        raise InvariantViolation(f"{ev.kind} stood down {snap.state}")
    if ev.kind == SQUEEZE and is_alerting(snap.state) and d.to_state != snap.state:
        raise InvariantViolation("a device squeeze changed an escalation")


def device_cmd(public_state: str, challenge_n: Optional[int], challenge_id: Optional[str],
               prompt_cause: Optional[str]) -> dict:
    """Device command derived from the public state only (never the masked one)."""
    if public_state in PROMPT_STATES and challenge_n:
        routine = challenge_n == ROUTINE_K and prompt_cause in ("routine", "session_end")
        return {"led": "amber", "blink_n": 0 if routine else int(challenge_n), "challenge_id": challenge_id}
    if is_alerting(public_state):
        return {"led": "red", "blink_n": 0, "challenge_id": None}
    return {"led": "off", "blink_n": 0, "challenge_id": None}
