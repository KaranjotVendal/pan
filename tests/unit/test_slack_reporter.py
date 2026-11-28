from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from slack_fakes import RecordingSlack

from pan.core.errors import SlackPostError
from pan.core.events import (
    AnswerReady,
    ChoiceUnclear,
    CleanupFailed,
    Event,
    NoSessionMatches,
    NotDelivered,
    NotDeliveredReason,
    PrivateDetails,
    QuestionAsked,
    ScreenCaptured,
    SessionClosed,
    SessionNotice,
    SessionNoticeReason,
    SessionsListed,
    StartAction,
    StartFailed,
    StatusChanged,
    ThreadNotice,
    ThreadNoticeReason,
    WorkKept,
)
from pan.core.models import AgentStatus, Conversation, SessionRole, SessionSummary, StatusState
from pan.frontend.slack_reporter import SlackReporter, thread_permalinks

# Each event's exact Slack output: the user-visible contract the backend's events turn into.

_THREAD = "1700000000.000100"
_CONVERSATION = Conversation.slack(_THREAD, "C0TEST")
_NOW = datetime(2026, 7, 16, 10, 30, 0, tzinfo=UTC)
_NOW_SLACK_TIME = "<!date^1784197800^{time}|10:30 UTC>"


def _report(event: Event, slack: RecordingSlack | None = None) -> tuple[RecordingSlack, Any]:
    slack = slack if slack is not None else RecordingSlack()
    return slack, SlackReporter(slack).report(_CONVERSATION, event)


# slack_post escapes Slack's control characters outside code, as Slack needs them.
_DESK_ENDED_TEXT = (
    "pan-login has ended, and pan does not reopen sessions it did not start; start one with "
    "@pan new: &lt;task&gt;, or send to another with @pan to &lt;name&gt;: &lt;task&gt;"
)


@pytest.mark.parametrize(
    "event, expected_text",
    [
        (
            QuestionAsked(candidate_labels=["pan-login (folder login)"]),
            "Send this to pan-login (folder login)? Reply yes.",
        ),
        (
            QuestionAsked(candidate_labels=["pan-a", "pan-b", "pan-c"]),
            "Which session? Reply 1, 2 or 3:\n1. pan-a\n2. pan-b\n3. pan-c",
        ),
        (ChoiceUnclear(candidate_count=1), "Reply yes to send it, or anything else to cancel."),
        (ChoiceUnclear(candidate_count=2), "Reply 1 or 2 to choose, or anything else to cancel."),
        (
            ThreadNotice(reason=ThreadNoticeReason.THREAD_NOT_TIED),
            "this thread isn't tied to a session; @pan sessions lists them",
        ),
        (ThreadNotice(reason=ThreadNoticeReason.NO_SESSION), "This thread has no session."),
        (
            ThreadNotice(reason=ThreadNoticeReason.QUESTION_EXPIRED),
            "Not sent: that question expired. Send the message again.",
        ),
        (ThreadNotice(reason=ThreadNoticeReason.NOT_SENT), "Not sent."),
        (
            ThreadNotice(reason=ThreadNoticeReason.MESSAGE_UNUSABLE),
            "I couldn't act on that message; please send the request again.",
        ),
        (
            SessionNotice(reason=SessionNoticeReason.DESK_SESSION_ENDED, session_name="pan-login"),
            _DESK_ENDED_TEXT,
        ),
        (
            SessionNotice(reason=SessionNoticeReason.THREAD_UNTIED, session_name="pan-login"),
            "untied this thread from pan-login; the session was not stopped",
        ),
        (
            SessionNotice(reason=SessionNoticeReason.DESK_SESSION_UNTIED, session_name="pan-login"),
            "Untied this thread from pan-login; pan never closes sessions it didn't start.",
        ),
        (
            SessionNotice(reason=SessionNoticeReason.NOT_CLOSED_WORKING, session_name="pan-login"),
            "pan-login is still working; send `@pan done` when it finishes, "
            "or `@pan done --discard` to stop it now.",
        ),
        (
            SessionNotice(
                reason=SessionNoticeReason.NOT_CLOSED_WAITING_ON_PROMPT, session_name="pan-login"
            ),
            "pan-login is waiting on a prompt; answer it at the terminal and send `@pan done` "
            "again, or `@pan done --discard` to stop it now.",
        ),
        (
            SessionNotice(
                reason=SessionNoticeReason.FOLDER_OUTSIDE_WORKTREES, session_name="pan-login"
            ),
            "pan-login's folder is not in pan's worktree folder; pan never removes it.",
        ),
        (
            SessionNotice(
                reason=SessionNoticeReason.FOLDER_IS_MAIN_REPOSITORY, session_name="pan-login"
            ),
            "pan-login's folder is a main repository; pan never removes it.",
        ),
        (
            SessionNotice(reason=SessionNoticeReason.WORKTREE_UNREADABLE, session_name="pan-login"),
            "Could not read pan-login's worktree; kept it.",
        ),
        (
            SessionNotice(
                reason=SessionNoticeReason.PROPOSED_SESSION_GONE,
                session_name="pan-login (folder login)",
            ),
            "pan-login (folder login) is no longer running; not sent",
        ),
        (
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="pan-login"),
            "pan-login is busy; try again, or @pan check",
        ),
        (
            NotDelivered(
                reason=NotDeliveredReason.BUSY, session_name="pan-login", retry_choice="2"
            ),
            "pan-login is busy; reply 2 again later, or anything else to cancel.",
        ),
        (
            NotDelivered(reason=NotDeliveredReason.WAITING_ON_PROMPT, session_name="pan-login"),
            "pan-login is waiting on a prompt at the terminal; nothing was sent",
        ),
        (
            NotDelivered(
                reason=NotDeliveredReason.WAITING_ON_PROMPT,
                session_name="pan-login",
                retry_choice="yes",
            ),
            "pan-login is waiting on a prompt at the terminal; nothing was sent; "
            "reply yes again later, or anything else to cancel.",
        ),
        (
            StartFailed(action=StartAction.SPAWN, session_name="pan-login"),
            "spawn failed for stream pan-login",
        ),
        (
            StartFailed(action=StartAction.REOPEN, session_name="pan-login", name_in_use=True),
            "reopen failed for stream pan-login: stream name in use",
        ),
        (
            NoSessionMatches(selector="login"),
            'no session matches "login"; to start new work send `@pan new: <your text>`',
        ),
        (
            WorkKept(session_name="pan-login", uncommitted_files=1, unpushed_commits=2),
            "pan-login has 1 uncommitted file and 2 unpushed commits; kept it. "
            "Push or commit them, or send `@pan done --discard`.",
        ),
        (
            CleanupFailed(session_name="pan-login", worktree_path=Path("/tmp/wt/pan-login")),
            "Closed pan-login, but could not remove its worktree or branch; send `@pan done` "
            "to try again, or remove /tmp/wt/pan-login by hand.",
        ),
        (
            SessionClosed(
                session_name="pan-login",
                worktree_removed=True,
                branch_deleted=True,
                repo_known=True,
            ),
            "Closed pan-login and removed its worktree and branch.",
        ),
        (
            SessionClosed(
                session_name="pan-login",
                worktree_removed=True,
                branch_deleted=False,
                repo_known=True,
            ),
            "Closed pan-login and removed its worktree.",
        ),
        (
            SessionClosed(
                session_name="pan-login",
                worktree_removed=False,
                branch_deleted=True,
                repo_known=True,
            ),
            "Closed pan-login and removed its branch; its worktree was already gone.",
        ),
        (
            SessionClosed(
                session_name="pan-login",
                worktree_removed=False,
                branch_deleted=False,
                repo_known=True,
            ),
            "Closed pan-login; its worktree and branch were already gone.",
        ),
        (
            SessionClosed(
                session_name="pan-login",
                worktree_removed=False,
                branch_deleted=False,
                repo_known=False,
            ),
            "Closed pan-login; its worktree was already gone.",
        ),
    ],
    ids=[
        "question-one-candidate",
        "question-several-candidates",
        "choice-unclear-one",
        "choice-unclear-several",
        "thread-not-tied",
        "no-session",
        "question-expired",
        "not-sent",
        "message-unusable",
        "desk-session-ended",
        "thread-untied",
        "desk-session-untied",
        "not-closed-working",
        "not-closed-waiting-on-prompt",
        "folder-outside-worktrees",
        "folder-is-main-repository",
        "worktree-unreadable",
        "proposed-session-gone",
        "busy",
        "busy-retry-choice",
        "waiting-on-prompt",
        "waiting-on-prompt-retry-choice",
        "spawn-failed",
        "reopen-failed-name-in-use",
        "no-session-matches",
        "work-kept",
        "cleanup-failed",
        "closed-worktree-and-branch",
        "closed-worktree",
        "closed-branch",
        "closed-nothing-left",
        "closed-no-repo",
    ],
)
def test_a_line_event_is_posted_as_one_message_in_the_thread(
    event: Event, expected_text: str
) -> None:
    slack, returned = _report(event)

    assert slack.posts == [("C0TEST", _THREAD, expected_text)]
    assert (returned, slack.status_posts, slack.ephemerals) == (None, [], [])


def _status(**updates: object) -> StatusChanged:
    status = StatusChanged(
        state=StatusState.WAITING,
        session_name="pan-login",
        label="fix the <login> page",
        task_started_at=_NOW - timedelta(minutes=4),
        updated_at=_NOW,
        replies_in_thread=True,
        detail="Allow edit of src/app.py?",
    )
    return status.model_copy(update=updates)


_STATUS_BLOCKS = [
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Waiting for you*\nfix the &lt;login&gt; page\n_Allow edit of src/app.py?_",
        },
    },
    {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"pan-login · 4m elapsed · updated {_NOW_SLACK_TIME} · answers posted here",
            }
        ],
    },
]


def test_a_status_with_no_message_is_posted_as_blocks_and_returns_its_id() -> None:
    slack, returned = _report(_status())

    ((post),) = slack.status_posts
    assert (post.channel, post.thread_ts, post.blocks, post.text) == (
        "C0TEST",
        _THREAD,
        _STATUS_BLOCKS,
        "Waiting for you: fix the <login> page",
    )
    assert returned == "status-1"
    assert (slack.posts, slack.status_updates) == ([], [])


def test_a_status_naming_its_message_edits_it_in_place() -> None:
    slack, returned = _report(_status(status_message_id="1700000000.000900"))

    assert slack.status_updates == [
        ("C0TEST", "1700000000.000900", _STATUS_BLOCKS, "Waiting for you: fix the <login> page")
    ]
    assert (returned, slack.status_posts) == (None, [])


def test_a_status_whose_edit_fails_is_posted_anew() -> None:
    slack = RecordingSlack()
    slack.update_error = SlackPostError("message_not_found")

    _slack, returned = _report(_status(status_message_id="1700000000.000900"), slack)

    assert [post.blocks for post in slack.status_posts] == [_STATUS_BLOCKS]
    assert returned == "status-1"


def test_a_status_that_cannot_be_posted_raises() -> None:
    slack = RecordingSlack()
    slack.update_error = SlackPostError("down")
    slack.post_blocks_error = SlackPostError("down")

    with pytest.raises(SlackPostError):
        _report(_status(status_message_id="1700000000.000900"), slack)


@pytest.mark.parametrize(
    "event, expected_texts",
    [
        (AnswerReady(text="**Done.** See `app.py`."), ["*Done.* See `app.py`."]),
        (
            AnswerReady(text="a" * 3000 + "\n\n" + "b" * 3000),
            ["(1/2)\n" + "a" * 3000 + "\n", "(2/2)\n" + "b" * 3000],
        ),
        (
            ScreenCaptured(text="> ready\nall tests pass"),
            ["Screen text (no transcript yet):\n```\n> ready\nall tests pass\n```"],
        ),
    ],
    ids=["answer", "long-answer-in-parts", "screen"],
)
def test_an_answer_is_posted_alone_in_mrkdwn(event: Event, expected_texts: list[str]) -> None:
    slack, _returned = _report(event)

    assert slack.texts() == expected_texts


def test_private_details_go_to_the_requester_only() -> None:
    slack, _returned = _report(
        PrivateDetails(
            requester="U0TEST",
            session_name="pan-login",
            terminal_address="t7",
            inbox_thread_name="pan-login: fix the login page",
            inbox_thread_url="http://localhost:19441/#thread=abc",
            inbox_thread_known=True,
        )
    )

    assert slack.ephemerals == [
        (
            "C0TEST",
            _THREAD,
            "U0TEST",
            "Details for pan-login (only visible to you)\n"
            "Inbox thread: <http://localhost:19441/#thread=abc|pan-login: fix the login page>\n"
            "Terminal: t7",
        )
    ]
    assert slack.posts == []


def _summary(name: str, thread_ts: str | None) -> SessionSummary:
    return SessionSummary(
        address=f"t-{name}",
        name=name,
        cwd=Path(f"/tmp/{name}"),
        agent="claude-code",
        agent_status=AgentStatus.WORKING,
        thread_ts=thread_ts,
        thread_channel="C0TEST" if thread_ts else None,
        role=SessionRole.STANDALONE,
        project=name,
        project_root=f"t-{name}",
    )


class _CountingSlack(RecordingSlack):
    def __init__(self) -> None:
        super().__init__()
        self.permalink_requests: list[tuple[str, str]] = []

    def permalink(self, channel: str, ts: str) -> str | None:
        self.permalink_requests.append((channel, ts))
        return super().permalink(channel, ts)


def _summary_in(address: str, thread_ts: str | None) -> SessionSummary:
    return SessionSummary(
        address=address,
        cwd=Path("/tmp/x"),
        agent_status=AgentStatus.IDLE,
        is_pan_owned=True,
        thread_ts=thread_ts,
        thread_channel="C0TEST" if thread_ts is not None else None,
        role=SessionRole.STANDALONE,
        project="x",
        project_root=address,
    )


def test_thread_permalinks_asks_once_per_thread_and_drops_threads_without_a_link() -> None:
    slack = _CountingSlack()
    slack.permalinks["1700000000.000200"] = "https://example.slack.com/p2"
    sessions = [
        _summary_in("t1", "1700000000.000200"),
        _summary_in("t2", "1700000000.000200"),
        _summary_in("t3", None),
        _summary_in("t4", "1700000000.000300"),
    ]

    permalinks = thread_permalinks(slack, sessions)

    assert permalinks == {"1700000000.000200": "https://example.slack.com/p2"}
    assert slack.permalink_requests == [
        ("C0TEST", "1700000000.000200"),
        ("C0TEST", "1700000000.000300"),
    ]


def test_the_sessions_list_goes_to_the_requester_with_each_thread_linked_once() -> None:
    slack = RecordingSlack()
    slack.permalinks = {"1700000000.000200": "https://example.slack.com/archives/C0TEST/p1"}
    sessions = [
        _summary("pan-login", "1700000000.000200"),
        _summary("pan-api", None),
    ]

    _report(SessionsListed(requester="U0TEST", sessions=sessions, any_running=True), slack)

    assert slack.ephemerals == [
        (
            "C0TEST",
            _THREAD,
            "U0TEST",
            "*Your sessions*\n"
            "• *pan-login* · Working · <https://example.slack.com/archives/C0TEST/p1|thread>\n"
            "• *pan-api* · Working",
        )
    ]
    assert slack.posts == []


@pytest.mark.parametrize(
    "event",
    [
        ThreadNotice(reason=ThreadNoticeReason.NOT_SENT),
        AnswerReady(text="done"),
        PrivateDetails(requester="U0TEST", session_name="pan-login"),
        _status(),
    ],
    ids=["line", "answer", "private-details", "status"],
)
def test_a_conversation_with_no_channel_raises(event: Event) -> None:
    with pytest.raises(SlackPostError):
        SlackReporter(RecordingSlack()).report(Conversation.slack(_THREAD), event)


@pytest.mark.parametrize(
    "event",
    [
        PrivateDetails(requester="U0TEST", session_name="pan-login"),
        SessionsListed(requester="U0TEST", sessions=[], any_running=True),
    ],
    ids=["private-details", "sessions-list"],
)
def test_a_private_message_that_fails_raises_and_posts_nothing_publicly(event: Event) -> None:
    slack = RecordingSlack()
    slack.ephemeral_error = SlackPostError("user_not_in_channel")

    with pytest.raises(SlackPostError):
        _report(event, slack)

    assert (slack.posts, slack.ephemerals) == ([], [])
