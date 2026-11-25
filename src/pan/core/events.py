from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from pan.core.models import SessionSummary, StatusState

# What happened in a conversation, as the backend reports it through a Reporter (core.seams).
# Each event says what happened and carries the facts a reporter needs to show it; none holds
# wording or markup, which belong to the reporter (frontend.slack_reporter for Slack). Every
# event is a frozen pydantic model; no wire format is fixed for them yet.


class StatusChanged(BaseModel, frozen=True):
    # pan's one status message in the conversation, at `state`. `status_message_id` is the
    # status message already posted (ThreadRecord.status_message_ts), edited in place when
    # the reporter can; with none, or when the edit fails, the reporter posts a new one and
    # returns its id. `detail` is what a waiting session waits for (the prompt's text).
    state: StatusState
    session_name: str
    label: str | None = None
    task_started_at: datetime | None = None
    updated_at: datetime
    replies_in_thread: bool = False
    detail: str | None = None
    status_message_id: str | None = None


class AnswerReady(BaseModel, frozen=True):
    # The session's answer, posted alone as it is (in parts when long).
    text: str


class ScreenCaptured(BaseModel, frozen=True):
    # A session with no transcript: its recent screen lines, already cleaned.
    text: str


class PrivateDetails(BaseModel, frozen=True):
    # The session's terminal and Inbox thread, for the requester alone: they are on this
    # machine and mean nothing to anyone else in the conversation. `inbox_thread_known` is
    # False for a name pan only suggested to a session it did not start.
    requester: str
    session_name: str
    terminal_address: str = ""
    inbox_thread_name: str | None = None
    inbox_thread_url: str | None = None
    inbox_thread_known: bool = False


class SessionsListed(BaseModel, frozen=True):
    # `pan sessions --show`: the sessions picked, in order, for the requester alone.
    # `any_running` is False when no session is running at all.
    requester: str
    sessions: list[SessionSummary]
    any_running: bool


class QuestionAsked(BaseModel, frozen=True):
    # pan asks which session should get a task: the proposed sessions' labels, most likely
    # first (one label asks yes or no).
    candidate_labels: list[str]


class ChoiceUnclear(BaseModel, frozen=True):
    # The reply to pan's question picked none of its `candidate_count` sessions; asked again.
    candidate_count: int


class ThreadNoticeReason(StrEnum):
    # Notices about the conversation itself, naming no session.
    THREAD_NOT_TIED = "thread_not_tied"
    NO_SESSION = "no_session"
    QUESTION_EXPIRED = "question_expired"
    NOT_SENT = "not_sent"
    MESSAGE_UNUSABLE = "message_unusable"


class ThreadNotice(BaseModel, frozen=True):
    reason: ThreadNoticeReason


class SessionNoticeReason(StrEnum):
    # Notices about one session, named by `session_name`.
    DESK_SESSION_ENDED = "desk_session_ended"
    THREAD_UNTIED = "thread_untied"
    DESK_SESSION_UNTIED = "desk_session_untied"
    NOT_CLOSED_WORKING = "not_closed_working"
    NOT_CLOSED_WAITING_ON_PROMPT = "not_closed_waiting_on_prompt"
    FOLDER_OUTSIDE_WORKTREES = "folder_outside_worktrees"
    FOLDER_IS_MAIN_REPOSITORY = "folder_is_main_repository"
    WORKTREE_UNREADABLE = "worktree_unreadable"
    PROPOSED_SESSION_GONE = "proposed_session_gone"


class SessionNotice(BaseModel, frozen=True):
    reason: SessionNoticeReason
    session_name: str


class NotDeliveredReason(StrEnum):
    BUSY = "busy"
    WAITING_ON_PROMPT = "waiting_on_prompt"


class NotDelivered(BaseModel, frozen=True):
    # Nothing was typed into the session. `retry_choice` is set when the text was the answer
    # to pan's question (the choice the person made), which they can send again later.
    reason: NotDeliveredReason
    session_name: str
    retry_choice: str | None = None


class StartAction(StrEnum):
    SPAWN = "spawn"
    REOPEN = "reopen"


class StartFailed(BaseModel, frozen=True):
    # A session could not be started (or reopened); `name_in_use` when a live terminal
    # already holds its name.
    action: StartAction
    session_name: str
    name_in_use: bool = False


class NoSessionMatches(BaseModel, frozen=True):
    # No live session is what the person named as `selector`.
    selector: str


class WorkKept(BaseModel, frozen=True):
    # `done` kept the session: closing it would lose uncommitted files or unpushed commits.
    session_name: str
    uncommitted_files: int
    unpushed_commits: int


class CleanupFailed(BaseModel, frozen=True):
    # `done` removed the terminal but not the worktree or branch.
    session_name: str
    worktree_path: Path


class SessionClosed(BaseModel, frozen=True):
    # `done` closed the session; says what this run removed. `repo_known` is False when pan
    # found no repository to delete the branch from.
    session_name: str
    worktree_removed: bool
    branch_deleted: bool
    repo_known: bool


Event = (
    StatusChanged
    | AnswerReady
    | ScreenCaptured
    | PrivateDetails
    | SessionsListed
    | QuestionAsked
    | ChoiceUnclear
    | ThreadNotice
    | SessionNotice
    | NotDelivered
    | StartFailed
    | NoSessionMatches
    | WorkKept
    | CleanupFailed
    | SessionClosed
)
