# Changelog

All notable changes to `pan` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
Semantic Versioning. The Night Shift build agent appends one entry per task under `## [Unreleased]`
as it works.

## [Unreleased]

### Changed

- The orchestrator follows the orchestrating skill installed with the running pan, not a path
  relative to its folder. `pan.orchestrating_skill` gives the packaged `SKILL.md`'s absolute
  path and a version (the first 12 hex characters of its sha256). The boot brief
  (`orchestrator_boot_brief`, formerly `ORCHESTRATOR_BOOT_BRIEF`) and every wake from the watcher
  and `pan up` (`wake_instruction`, formerly `WAKE_INSTRUCTION`) name both, and the wake tells
  the orchestrator to read the file again when the version is not the one it last read. Testing
  a branch now needs only `uv tool install <checkout> --force` and `pan service install`. When
  the skill file cannot be read, pan logs it and still boots or wakes the orchestrator, with
  text that names the path without a version and asks it to read the file again. The texts now
  say "decide what each person means and act through `pan` subcommands" instead of
  "classify and route".
- The orchestrator decides what a message means; code keeps only the actions and the safety
  rules. The gateway no longer parses route words: it writes the raw message to the inbox (the
  `@pan help` fast path and the untagged-reply rule are unchanged). `pan inbox drain --json` now
  gives each item as `item`, `text` (the message with the mention, a trailing client signature
  and phone punctuation cleaned; `clean_message`, formerly `_clean_message`), `tie` and
  `open_question` (pan's open `pan propose` question in the thread, with
  `answered_by_requester` and `answer_text`). The `route`, `directive` and
  `cancels_confirmation` fields are gone; an inbox item an older gateway wrote with a `route`
  still loads. `InboxItem.mentioned` is True for an `app_mention` and False for a reply the
  gateway forwarded without one (True for older items).
- `pan inbox drain` keeps every drained item for 24 hours, at most 1000
  (`FileDrainedLog`, `drained.jsonl` next to the thread map), so the gates read what the person
  typed from pan's own record (written under an exclusive lock). `--item` must be the newest
  item pan drained in its thread and channel from the same sender (a teammate's later "+1" does
  not block it). When pan cannot use `--item` (unknown, not the sender's newest, not from the
  requester) it posts "I couldn't act on that message; please send the request again."
  (`UNUSABLE_ITEM_REPLY`) in the thread. `pan send --to` and `pan relay` (which now takes
  `--thread` and `--channel`) take `--item <item id>` and exit 28 (nothing sent or posted) unless that item was sent in this
  thread and channel (by `--requester` when given) and holds the name as a whole word or phrase,
  any letter case, as well as matching exactly one live session as before. `pan propose`
  requires `--item` from `--requester` (exit 28 otherwise).
- `pan done --discard` takes `--item` and exits 33 (`DiscardNotRequestedError`), touching and
  posting nothing, unless that item is the thread's newest, was sent by the thread's requester
  (`ThreadRecord.requester`) and is exactly `done --discard` (a phone's `—discard` counts).
- Confirmations: the orchestrator reads the reply to a "Send this to ...?" question.
  `pan inbox drain` records the requester's latest reply in its thread after pan last asked
  (`PendingConfirmation.asked_at`, `answered_at`, `answer_text`). A question stored without a
  requester (before pan kept one) is never answered and simply expires after 30 minutes.
  `pan confirm --choice` (`yes`, `y` or 1-3) exits 34 (`UnansweredQuestionError`), sending and
  posting nothing, with no reply, a reply that declines ("no", "nope", "cancel", "none of
  those", ...), `yes` chosen but a reply that is not a plain yes (a yes word with only
  punctuation, "please", "thanks", "go ahead", "send it" or "that one" after it), or a number chosen but a
  reply not starting with it ("2", "2 please", "#2"; "not 2, 3" picks nothing). Re-asking (yes
  to several, a number out of range) and refusing a busy or waiting session clear the reply and
  reset `asked_at`. A question expires 30 minutes after pan last asked, so asking again keeps it
  open.
  Expiry, range and still-live checks are unchanged; a decline is `--cancel`. `pan propose`
  requires `--requester`.
- A thread from X may only start new sessions: `pan send` (with or without `--to`),
  `pan propose`, `pan relay` and `pan reopen` exit 35 (`XSourceRefusedError`, `X_TO_ROUTE_NOTICE`) when the thread record or
  the `--item` comes from X. The X safety gate itself is still to come (MOB-87).
- `pan sessions --match` is replaced by `--show <address,...>` with `--thread`, `--channel` and
  `--requester`: the orchestrator picks the sessions and their order, pan sends at most 5 of them
  to the requester alone.
- Public posts and edits (`post_message`, `post_blocks`, `update_message`, block texts included)
  cut local paths under `/Users`, `/home` and `~`, and `file://` URLs (any letter case, with a
  path after the scheme), to `.../<last part>` (`redact_paths`), in code too. A path continues
  past one space only when the segment before it has no file extension and the next run holds a
  `/` and starts no new path or URL, so `.../Mobile Documents/.../draft.pdf` is cut whole while
  "a.py and/or ~/b.py" is not joined. A Slack link whose target starts with `/`, `~`, `.` or
  `file:` is replaced by its label, or the target's last part; other links and mentions keep
  their target and have paths cut from their label. Ephemeral messages to the
  requester are not cut.
- The orchestrating skill is rewritten (about 140 lines): what each inbox item gives, the
  actions with their exit codes, and the safety rules. The Slack help footer describes requests
  in plain words.

### Removed

- `parse_directive`, `parse_route`, `route_for_source`, `parse_confirm_reply`, the retired-flag
  notices, `TaskMode`, `RouteKind`, `Route`, `Directive`, `InboxItem.route`, `OpenQuestions`,
  `route_with_pending`, `select_sessions_for_slack`, `match_words` and
  `SESSIONS_MATCH_MIN_WORD_LENGTH`.
- Unused code, with no change to anything in use: the dormant approval gate
  (`src/pan/hooks/pretooluse_gate.py`, `GatedOpDeniedError` and its exit code 18, the `gated_ops`
  config key, `UserPolicy.autonomy`, `UserPolicy.repos` and the `Autonomy` enum), which never
  enforced anything; the `pan pause` command, whose flag nothing read (`~/.pan/paused.flag` can be
  deleted); `src/pan/power.py` (`should_caffeinate`, never wired); the `defaults` config section
  (`Defaults`, with `agent`, `permission_mode` and `repo_allowlist`), the `Agent` enum,
  `PanPaths.logs` and `ThreadRecord.agent`, which nothing read; and `parse_inbox_link`, which only
  tests called (`pan hook inbox-link` is unchanged). A config that still holds the removed keys
  (`defaults`, `gated_ops`, `paths.logs`, `users.*.autonomy`, `users.*.repos`) loads as before:
  `load_config` ignores them and logs one warning naming them. A thread record with `agent` loads
  as before; the key is ignored.

### Fixed

- The gateway forwarded every human thread reply without an `@pan` mention, in any channel the
  bot is in, so a reply in an unrelated thread started a new session there. A reply without a
  mention is now forwarded only from an allowed sender in a thread pan is already working in: one
  tied in the thread map (same Slack channel), or one the gateway just accepted an `@pan` mention
  in and that is not tied yet (remembered in memory, last 500 threads). Anywhere else it is ignored
  and starting work still needs `@pan`; an unreadable thread map drops the reply with a warning.
  Replies with an attachment (`file_share`) or also sent to the channel (`thread_broadcast`) are
  forwarded too. The gateway command injects the thread map into `BoltSlackAdapter`
  (`thread_map=`).
- Follow-ups typed into a session come from the pan-orchestrator terminal, and a worker took them
  for another agent and answered with `hc send`. The worker brief and the "from Slack" line typed
  into a desk session now end with one sentence (`RELAYED_FOLLOW_UP_SENTENCE`): messages from
  pan-orchestrator are the person's follow-ups relayed from Slack; answer them as usual and never
  reply with `hc send`.
- A worker's brief and the "from Slack" line typed into a desk session now say who replies in
  Slack, by the thread's reply mode, instead of a bare "Do not post to Slack." that a worker in
  the reply-in-Slack mode read as a refusal. In that mode they say "Do not post to Slack
  yourself: pan posts your final answer to this Slack thread for you."; otherwise "Do not post
  to Slack yourself: the Slack thread gets a reply only when the person asks for one, and pan
  posts it for you." `worker_brief` and `from_slack_text` take the reply mode, and a follow-up
  to a desk session uses its record's mode.
- `check` on a session with no transcript posts its screen text cleaned by `clean_screen_text`:
  lines made only of box-drawing or rule characters (Claude Code's dividers and banner frame)
  are dropped, runs of blank lines collapse to one, trailing whitespace is stripped, and only
  the last `CHECK_SCREEN_MAX_LINES` (30) non-empty lines are kept. The "(incomplete: ...)"
  marker stays on top.
- The inbox-link hook now records the Inbox link for a worker in Claude Code's code mode, which
  calls the Inbox from inside its `REPL` tool. The worker's PostToolUse matcher is
  `^(mcp__plannotator__plannotator_inbox|REPL)$` (built from `PLANNOTATOR_INBOX_TOOL_NAME` and
  the new `REPL_TOOL_NAME`), and the hook skips, with a log line, any other tool name. A REPL
  call counts only when its code holds `plannotator_inbox` (any accessor) or its result holds
  the Inbox's send line. A REPL result yields a link only from a send line at the start of a
  line of its top-level text ("Sent to the Plannotator Inbox (thread <id>, <url>)", also
  "Already sent ..." and "... asking 1 question ..."), never from JSON or nested message
  bodies, so a thread read or listing is never recorded. The direct tool reads its JSON result
  first and the send line second. Send lines naming more than one thread record nothing. A
  recorded link is kept unless the send names the record's own Inbox thread: the direct tool's
  `thread` input, or for a REPL call an exact `thread: "<name>"` argument in its code.
- A session record joins a live terminal only when it belongs to it, in one rule shared by
  `to <name>:` (`_session_record_for`) and `pan sessions` (`_match_record`): by address only
  when the names also agree (hc can reuse an address but cannot rename a terminal), and by name
  only for a record with no address (legacy records) or a pan worker back at a new address in
  its own worktree. The worktree fallback joins only records with no address. `check` and
  `pan read --full` also require the names to agree at the recorded address. A desk session in
  a new terminal named like a killed one no longer reuses the old record's Inbox thread name.
- A trailing client signature such as "\n*Sent using* <@U0EXAMPLE|Claude>" (added by some Slack
  clients and connectors, any letter case, with optional `*`/`_` emphasis, `>` quote and `|label`)
  is stripped in `_clean_message` before route parsing, so `check`, `sessions` and `forget` match
  again and the signature never reaches task text, briefs or Inbox thread names.
- The inbox-link hook now finds the link in the Inbox's real send result, which is a prose line
  followed by the JSON result (it only parsed text that was JSON as a whole). Every early return
  of the hook entrypoints logs why (no cwd in the payload, or no record for `cwd=<path>`), and a
  `~` in the cwd is expanded.
- The Notification hook no longer marks a thread blocked for Claude Code's idle reminder
  ("Claude is waiting for your input", `notification_type` `idle_prompt`). Only a prompt waiting
  on the person (`permission_prompt`, `elicitation_dialog`, or with no known type a message about
  permission or approval) marks it blocked or posts.
- A brand-new Claude session, which reports no status until its first turn ends, is no longer
  refused as busy: no status counts as ready for a known Claude agent (`claude`, `claude-code`)
  at turn 0 or with no turn count. Shells and other programs with no status, unknown states and
  working sessions are still refused. `Terminal` gains `turn`; the hc adapter reads it, and names
  the agent of a status-less `hc get` entry by its launched program.
- The Inbox lookup by name is documented and tested against the live listing shape. The desk
  session's lookup missed because it ran right after the task was typed, before the session had
  created its thread; `check` looks again until it is found. `project_path` works for any folder:
  a git worktree maps to its main repository's project.
- Tests no longer write to the real `~/.pan/logs/pan.log`: `PAN_LOG_DIR` points the log file at
  another folder, and `tests/conftest.py` sets it to a throwaway folder before pan is imported.

### Added

- A new session started from a reply inside a Slack thread reads that thread first. The
  orchestrator passes the new `pan spawn --thread-reply` when the inbox item's `is_thread_reply`
  is true, and the worker brief then adds a paragraph (`slack_thread_paragraph`) naming the
  channel and thread ts and telling the session to read the whole thread, every message and every
  page, with the Slack read-thread tool (`slack_read_thread`, `SLACK_READ_THREAD_TOOL_NAME`), only
  to read and never to post; if the tool is unavailable it says so in its Inbox thread and goes on
  with the task text. pan itself does not fetch the thread. Top-level mentions, follow-ups, reopens
  and `to` sends are unchanged.
- A follow-up can turn on replies in Slack. `pan send` without `--to` (`follow_up_to_session`) and
  `pan reopen` (`reopen_worker`) take `--reply-in-slack`: it sets the thread record's reply mode to
  slack (`upgraded_reply_mode`), saved only once the delivery succeeds: right after the text is
  typed (the new `ThreadMap.set_reply_mode`), or with the reopened record's tie after the launch.
  The Stop hook then posts the session's answer to the thread, and the mode stays for the thread.
  A busy or blocked refusal, or a failed reopen, leaves the record unchanged. Without the flag the
  mode is left as it is; it never goes back to on demand. A desk session's typed "from Slack" line
  uses the new mode. SKILL.md step (b) passes the
  flag on follow-ups and reopens under the same "reply in Slack by meaning" rule ("reply back with
  the link", "post it here"). Before, a follow-up such as "reply back with the link" in a thread
  started without it reached the session but its answer stayed in the terminal.
- The orchestrator passes `--reply-in-slack` (to `pan spawn`, `pan send` and `pan propose`) when a
  message plainly asks for its result in the Slack thread ("share it here", "post it here", "send
  the link here", "reply here"), as well as when it says "reply in slack". `wants_slack_reply`
  stays the code-level hint; the new judgment is the orchestrator's second bounded exception to
  INV-3, stated in SKILL.md next to the confirm exception, and it sets the reply mode only.
- `@pan done` closes the thread's session. New route word `done` (`RouteKind.DONE`), the whole
  message like `check` and `forget`, and `done --discard` (`Route.discard`; a phone's `—discard`
  works too). New `pan done --thread --channel [--discard]` (`session_control.done_thread`) and
  orchestrator step (r5) (the later steps are now (r6) to (r9)). An untied thread gets "This thread
  has no session."; a session pan did not start is only untied. For pan's own session it refuses
  while the session is working (exit 26) or waiting on a prompt (exit 23), and keeps everything
  while there is unsaved work (new `UnsavedWorkError`, exit 29): modified files, untracked files
  other than the untracked `.claude/settings.json` pan writes at spawn
  (`WORKER_SETTINGS_RELATIVE_PATH`), commits only a detached HEAD holds, or branch commits the
  upstream lacks (an upstream gone after a merge counts as pushed; with no upstream, commits merged
  into neither `origin/HEAD` nor the main checkout's branch), unless `--discard`. Otherwise it
  removes the terminal, the worktree and the branch, and only then marks the record DONE and
  unties the thread. With the worktree folder already gone it prunes and deletes the branch from
  the repository, found from the new `ThreadRecord.repo_path` (stored at spawn; older records fall
  back to the orchestrator's folder only when that repository has the branch). A failed git
  cleanup (new `GitWorktreeError`, exit 30) keeps the thread tied with its status, clears the
  removed terminal's address and asks for a retry; a folder outside the worktree folder or a main
  checkout is never removed (new `UnsafeWorktreeError`, exit 31). The `GitWorktreeAdapter` seam
  gains read-only `worktree_status` (`WorktreeStatus`) and `branch_status` (`BranchStatus`),
  `prune_worktrees` and `delete_branch`, and `remove_worktree` now takes `force`. `pan stop` is
  unchanged.
- `to <name>:` with no exact match asks before sending. The name stays one token, so a sentence
  such as "to be clear: ..." is still a plain task. `pan send --to` still sends at once to an exact
  match (address, terminal name, or Herdr label or space); otherwise it sends and posts nothing and exits
  28 (`NoExactMatchError`; a label several sessions share counts as no exact match and no longer
  posts a pick list, and the close-matches not-found reply is gone). The orchestrator then reads the
  message and `pan sessions --json` and proposes the likely session, up to 3, or none, with the new
  `pan propose --thread --channel --to --task [--requester] [--reply-in-slack] --candidate <address>
  ...`. It keeps only live terminal addresses (unknown or repeated ones are dropped, at most
  `CONFIRMATION_MAX_CANDIDATES`, 3), stores the question per thread in `confirmations.json` next to
  the thread map (`PendingConfirmation`, seam `PendingConfirmations`; not on `ThreadRecord`, because
  a `to` in a new thread has no record) and posts "Send this to stream-pan (work / 1, folder pan)?
  Reply yes.", or "Which session? Reply 1, 2 or 3:" with a numbered list; with no candidate it posts
  'no session matches "<selector>"; to start new work send `@pan new: <your text>`' and stores
  nothing. Nothing is typed into a session until the person replies.
- Replies to that question are decided in code. `pan inbox drain` turns a `yes`, `y` or 1-3 reply
  (`directive.parse_confirm_reply`, any case, optional mention) in a thread with a question younger
  than `CONFIRMATION_TIMEOUT` (30 minutes) into the new route kind `confirm` (`Route.choice`), and
  turns a decline (`no`, `n`, `cancel`, `stop`) into `confirm` with choice `no`, which
  `pan confirm --cancel` (or `--choice no`) answers with "Not sent." and nothing more, so a "no" in
  an untied thread never starts a session; it marks any other message there
  `cancels_confirmation`; the questions are read before the inbox is drained, an unreadable `confirmations.json` counts as none (no item is ever lost to it), and once
  an item answers or cancels a thread's question, later items in the same batch see none. A plain
  `yes` with no open question is unchanged (it still goes to the tied session). The new
  `pan confirm --choice <yes|y|n>` (read like a reply: any case, spaces trimmed) sends the stored
  task to the chosen session through the `to` send path and ties the thread, after checking it is
  still the same live terminal (same address and name, or for an unnamed session the same folder
  and Herdr label, kept on `ConfirmationCandidate`); the question is cleared only once the send
  succeeds. A busy session or one waiting on a prompt keeps the question open ("stream-pan is busy;
  reply yes again later, or anything else to cancel."); a chosen session no longer running clears
  it. `yes` to several or an out-of-range number asks again and keeps the question; a question that
  expired is cleared with "Not sent: that question expired."; with no question at all nothing is
  posted. `pan confirm --cancel` clears the
  question and posts "Not sent.", and the message is then handled as usual. The orchestrating skill
  states this as the one bounded exception to INV-3.
- A session pan did not start is told to use its existing Plannotator Inbox thread for the work if it
  has one, otherwise to start one named `<session>: <task>`, unless pan knows its thread (found
  earlier, including by a lookup of the suggested name just before typing; that lookup is not
  repeated after the tie). A thread found that way is recorded on the thread record (name and id) so
  `check` uses it. pan's own workers are unchanged. Whether pan knows a session's thread is one rule,
  `sessions.inbox_thread_known`, shared with `pan sessions`.
- `pan sessions` shows each session's Inbox thread under its row when pan knows it
  (`SessionSummary.inbox_thread_name`), which also helps the orchestrator propose a session.
- `pan service install|uninstall|status`: pan starts by itself after a login or reboot on macOS.
  `install` renders the gateway, watcher and up LaunchAgents into
  `~/Library/LaunchAgents/com.pan.<name>.plist` (home directory and the absolute path of the
  installed `pan` filled in), creates `~/.pan/logs`, and loads each with `launchctl bootstrap
  gui/<uid>` in the order gateway, watcher, up; a loaded agent is booted out first, so running it
  again reinstalls. `uninstall` boots each out (a job that is not loaded is skipped) and deletes its
  plist. `status` prints, per agent, whether its plist is installed, whether it is loaded, its pid and
  its last exit code (from `launchctl print`). launchctl and the plist files sit behind the
  `ServiceManager` seam (`src/pan/adapters/launchd.py`); a launchctl failure raises `ServiceError`
  (exit 27). The templates moved from `launchd/` into the package (`src/pan/launchd/`) so an
  installed pan finds them, and the program path is now a `<PAN_BIN>` placeholder. `service` is
  left out of the Slack help listing, like `gateway` and `watcher`.
- Reaching sessions pan did not start ("desk sessions"). `to <name>:` (`pan send --to`) now reaches
  any live session. A desk session gets a thread record with `started_by_pan` false and its own
  folder as `worktree_path`, tied to the thread (one thread per session still holds).
- Busy sessions are refused. Before typing into any existing session (`to <name>:` or a follow-up),
  pan reads its state from the terminal host: idle (or done) is typed into; busy, an error, no
  status or an unknown state is refused with "<name> is busy; try again, or @pan check"
  (`SessionBusyError`, exit 26); waiting on a prompt keeps the blocked reply. Nothing is typed on a
  refusal.
- `pan send` without `--to` sends a follow-up to the thread's tied session (`follow_up_to_session`)
  with the same checks. When the session has ended it exits 22: the orchestrator reopens pan's own
  sessions; for a desk session pan says it ended and suggests `new:` or `to <name>:`.
- Text typed into a session from `to <name>:`, and every follow-up to a desk session, gets one line
  on top: "From Slack via pan", the Plannotator Inbox thread to use (`<name>: <short task>`, or the
  one pan named before), and that Slack hears back only when asked (`from_slack_text`).
- The `InboxDirectory` seam and `ShellPlannotatorInboxAdapter` (`src/pan/adapters/plannotator_inbox.py`)
  look an Inbox thread up by name with `plannotator inbox call read_thread` (`asked_by: "anyone"`,
  the session's folder as `project_path`). Read only, never sends; any failure returns nothing.
  `check` and the "Sent to …" notice use it for sessions whose Inbox thread id is unknown; the
  listing carries no link, so usually only the name is shown. Needs a live check.
- Messages from X may not use `to <name>:` until the X safety gate (MOB-87): `parse_route` takes the
  source and `route_for_source` turns such a route into a notice, at parse time and again at drain.

- Route words, parsed in code by `parse_route` (`src/pan/directive.py`): `new: <task>`,
  `to <name>: <task>`, and the whole-message words `check`, `sessions` and `forget`. Strict: the
  route word is the first word after the mention, spelled exactly, in any letter case, after the
  phone-punctuation repair; anything else is task text. "reply in slack" anywhere sets
  `reply_in_slack` on the route.
- The gateway parses the route and writes it on the inbox item; `InboxItem` gains `source` and
  `route`. INV-1 now reads: the gateway parses route words, and still decides nothing about the
  work. `pan inbox drain --json` emits each item's `route` (parsed at drain for an item an older
  gateway wrote) and the directive parsed from the route's text.
- `pan check --thread … --channel …` posts the tied session's latest answer as is: the last answer
  in its transcript, or its recent screen lines labelled as screen text. It names the Inbox thread
  (link, or name before the link is known) and says when the session is still working, waiting on a
  prompt at the terminal, or ended. Long answers are posted in labelled parts (`split_for_slack` in
  `src/pan/slack_format.py`, `slack_post_parts` in `src/pan/gateway/slack_post.py`). An untied thread
  gets "this thread isn't tied to a session; @pan sessions lists them".
- `pan send --thread … --channel … --to <name> --task …` (the `to <name>:` route) sends the task to
  a live session and ties the thread to it, posting "Sent to <name> · Inbox: …". An unknown name gets
  a reply listing close matches, an ambiguous one lists the candidates, a blocked one says it is
  waiting on a prompt; nothing is sent. For now only sessions pan started can be named; any other
  live session gets "<name> is not a pan session; reaching your own sessions comes next"
  (`NotPanSessionError`, exit 25).
- `pan forget --thread … --channel …` unties the thread and says so; it never stops the session.
- `pan sessions` shows the last thing each session was asked (`last_ask`, from its transcript when
  pan knows it; a worker's brief is cut back to its task), and with `--thread` and `--channel` posts
  the table to a Slack thread. `last_user_text` in `src/pan/transcript.py` reads it, skipping tool
  results, meta and sidechain entries, and text Claude Code writes itself.

- Thread records carry session-control fields: `source` (`slack`; room for `x`), `tied`,
  `started_by_pan`, `requester`, `reply_mode` (`on_demand` by default, or `slack`), and the
  session's Plannotator Inbox thread (`inbox_thread_name`, `inbox_thread_id`, `inbox_thread_url`).
  Records written before them load with defaults. `FileThreadMap` gains `tie` (one thread per
  session: tying a session to a new thread unties its old one), `untie` and `record_inbox_thread`.
- `pan hook inbox-link`, a PostToolUse hook on `mcp__plannotator__plannotator_inbox` in the worker
  settings. It reads the Inbox thread id and link from the tool result and stores them on the
  worker's thread record. It never posts to Slack and always exits 0. The payload shape needs a live
  check.
- `wants_slack_reply` (`src/pan/directive.py`): "reply in slack" anywhere in a request, any letter
  case, sets the new thread's reply mode to `slack`. `pan spawn` takes `--requester`,
  `--request-text` and `--reply-in-slack`.
- An `idle` worker status: the session's turn ended and it waits for a follow-up.

### Changed

- `check` picks the status message's state from the live terminal (`check_state`): hc's `done` and
  `idle` are Finished (a finished turn), `working` is Working, `blocked` is Waiting for you, an
  agent error is Stopped, and no status or an unknown one shows what pan recorded, never Closed or
  Stopped while the terminal is live. A terminal that is gone is Stopped, or Closed when pan
  recorded it closed by `@pan done`. Before, a live idle session whose hc status was `done` showed
  Closed.
- An answer is never posted twice. The Stop hook and `check` post it through `post_answer`, which
  stores a SHA-256 digest of it on the record (`last_answer_digest`, None on older records,
  `ThreadMap.record_posted_answer`) and skips an answer equal to the last one posted. In the
  reply-in-Slack mode `check` no longer reposts the answer the Stop hook already posted; on demand
  it posts it once. Screen text (no transcript) is not deduplicated.
- Slack output is redesigned so a shared thread is easy to read. Each thread gets one status
  message (Block Kit: the state in bold with the task label under it, cut to
  `STATUS_LABEL_MAX_LENGTH` (80), and a grey context line with the session name, the elapsed time
  and the update time) that pan edits in place with `chat.update`: Working (spawn, `to`/confirm,
  a follow-up, reopen), Waiting for you (the Notification hook, with the prompt's text), Finished
  (the Stop hook), Closed (`done`) and Stopped (`pan stop`, and `check` on an ended session). Its
  ts, the task label and the task's start are stored on the `ThreadRecord`
  (`status_message_ts`, `status_label`, `task_started_at`; None on older records). When it cannot
  be edited, pan posts a new one and stores that ts. A plain `text` fallback is always set. The
  rendering is pure, in `src/pan/slack_render.py`; posting and editing are in
  `src/pan/status_message.py`, best effort (a Slack failure is logged, never raised).
- The answer is its own message. In reply-in-Slack mode the Stop hook posts only the session's
  final answer, converted to Slack mrkdwn (now also `__bold__`), and nothing when there is no
  answer (no more "Worker finished."). `check` edits the status message and posts the latest
  answer alone (or nothing); the "pan-X · Inbox: ..." header and the "pan-X has finished." line
  are gone. The Notification hook no longer posts the prompt; it shows in the status message.
- Private details go only to the requester: the session's Inbox thread (with its `localhost`
  link), its name and its terminal address are sent with `chat.postEphemeral` at start (spawn,
  `to`/confirm) and on `check`, and skipped when the record has no requester. The start notice
  ("Started pan-X in a new worktree · Inbox thread: ..."), the "Sent to pan-X · Inbox: ..." notice
  and the "reopened — stream pan-X" ack are replaced by the status message.
- `SlackAdapter` gains `post_blocks` (returns the ts), `update_message` and `post_ephemeral`;
  `ThreadMap` gains `record_status_message` and `record_task`. `check_thread`, `done_thread` and
  `follow_up_to_session` take a clock. All three Slack calls use the existing `chat:write` scope.

- Thread records are kept per (thread, session) instead of per thread (`record_key`). `new:` in a
  tied thread, or `to <name>:` naming another session, no longer overwrites the old session's
  record: it stays, untied, and its hooks still find it by worktree. `FileThreadMap.get` returns the
  thread's tied record (else its newest); `get`, `update_status`, `record_session` and
  `record_inbox_thread` take `session_name` to pick one session's record. Files keyed by thread
  alone load as before and are rewritten in the new shape on the next write.
- The Stop and Notification hooks of a session whose thread moved to another session only update
  its own record; they post nothing to that thread.
- `pan stop` refuses for a session pan did not start (`NotPanSessionError`, exit 25): it unties the
  thread and never stops the session. `pan reopen` refuses such a session too. `pan sessions`
  shows a tied desk session as external, with its thread.
- `to <name>:` no longer refuses sessions pan did not start (the slice B notice "<name> is not a
  pan session; reaching your own sessions comes next" is gone).
- `--new`, `--status`, `--sync` and the leading `!` are retired. A message carrying one gets a short
  reply naming what replaced it (`new:`, `check` / `sessions`, `reply in slack`) and nothing else
  happens. `parse_directive` no longer reads them; `TaskMode` loses `sync` and `status` and
  `Directive` loses `force_new`. `relay` and `read` stay as one-off actions that tie nothing.
- The orchestrating skill acts on the route first: `check`, `sessions` and `forget` run the `pan`
  command that posts its own reply; `to <name>:` runs `pan send`; `new:` spawns; with no route words
  the tie decides as before. The Slack help footer and the README list the route words.
- Replies to Slack are on demand. The Stop hook marks the thread `idle` (not `done`) and posts the
  last answer only in reply mode `slack`, where it posts at every turn end as before. The
  Notification hook only marks the thread `blocked`, posting the prompt only in reply mode `slack`.
- `pan spawn` posts one start notice naming the session and its Inbox thread (`Started pan-7 in a
  new worktree · Inbox thread: "pan-7: <short task>"`) instead of "on it — stream ...". The worker's
  brief opens with a fixed preamble: use the Plannotator Inbox in a thread with exactly that name,
  send a first short message there before starting, and do not post to Slack. A failed spawn leaves
  the thread untied.
- `pan inbox drain` puts the thread's tie on each item (`tie` in `--json`, an indented line in text
  output): "this thread's work belongs to <session> (<status>); send follow-ups there; never start a
  second session for the same work".
- The orchestrating skill no longer guesses between reopening and spawning fresh. In a tied thread,
  follow-ups go to the tied session (relayed, or reopened when it ended); a plain `@pan` in an untied
  thread starts a new session; `--new` moves the tie to a new session. SYNC now spawns with
  `--reply-in-slack` instead of blocking.
- `pan status` and `pan sessions` report an idle hc agent as `idle`; `running` and `idle` never count
  as drift. `pan up` treats `idle` workers as live when looking for lost ones.

### Fixed

- The watcher woke the orchestrator 5 times for one Slack message: every filesystem event in the
  inbox folder sent a wake, including the append's temp-file writes and the orchestrator's own drain.
  It now forwards only a new `<id>.json` entry, and a trailing debounce
  (`WATCHER_DEBOUNCE_SECONDS`, 1 s) turns a burst into a single wake.

### Changed

- Put hc behind a neutral terminal host seam, with no behaviour change. The `HcAdapter` protocol is
  now `TerminalHost` (`src/pan/seams.py`); `ShellHcAdapter` is its hc implementation, built by
  `build_terminal_host` (`src/pan/adapters/terminal_host.py`) from the new config key
  `terminal_host` (only `"hc"`, the default; any other value is a config error, exit 11). Callers
  outside the adapter catch the new neutral `TerminalHostError` (exit 16); `HcError` moved into the
  hc adapter as its subclass, and `TerminalGoneError`, `TargetBlockedError` and
  `TerminalNameTakenError` now subclass `TerminalHostError`. Parameters named `hc` are now
  `terminal_host`. Ownership of a terminal stays decided by the thread map; labels are only a hint.
  `pan relay` now loads the config, to pick the terminal host.
- Migrated from the herdr TUI CLI and morcli to herdr-core (`hc`). herdr is no longer installed and
  `morcli streams` depends on it, so spawn, sessions, relay, read, status, stop and the watcher wake
  were all broken. A new `ShellHcAdapter` (`src/pan/adapters/hc.py`, seam `HcAdapter`) is the only
  place `hc` is shelled (BR-2); it maps hc JSON to `Terminal` / `TerminalRead` (INV-8) and hc's stderr
  error kinds to typed errors (`HcError`, exit 16; `TerminalGoneError` 22; `TargetBlockedError` 23;
  `TerminalNameTakenError` 24). `HerdrError` and `MorcliError` (exit 19) are gone.
- Workers start with one `hc run --bg --no-parent` per spawn: `claude <brief>` as the terminal's
  program (the brief is an argv element, never shell text), named `pan-<stream>` normalized to hc's
  name rules, labelled `pan.role=worker`, `pan.thread`, `pan.channel`, plus `herdr.space` when the new
  optional `orchestrator.app_space` is set (workers are otherwise hidden from the Herdr app).
  `--no-parent` keeps workers alive past the orchestrator and keeps hc notes out of it (INV-2). A
  live terminal already holding the name fails the spawn before any worktree is created.
- `ThreadRecord` now stores `terminal_name` and `terminal_address` (replacing `workspace_name`,
  `workspace_id`, `pane_ids`, `morcli_session`) plus `claude_session_id` and `transcript_path`.
  Herdr-era records still load, as lost: no terminal address and status `failed` unless `done`.
- Config: `orchestrator.pane_id` and `orchestrator.workspace_name` are replaced by
  `orchestrator.terminal` (an hc name, default `pan-orchestrator`), `orchestrator.cwd`, and optional
  `orchestrator.command` and `orchestrator.app_space`. An old config with `pane_id` fails with a
  message naming the replacement keys.
- `pan status` and `pan sessions` read live status from hc (`idle`, `working`, `done`, `blocked`,
  `error`, or `none`); a finished or missing worker terminal reads as `failed` (or `done`). `pan
  relay` and `pan read` target an hc address, a terminal name, or a Herdr app space label; relaying
  into a session waiting on a person exits 23 without typing anything. `pan read` flags an
  incomplete (screen-only) read; `pan read --full` renders the Claude transcript recorded by the
  worker's new SessionStart hook, falling back to labelled screen history for other terminals.
- The watcher wakes the orchestrator with one `hc send` and logs, rather than raises, when the
  orchestrator is blocked or missing, so launchd never crash-loops it. The gateway and watcher
  LaunchAgents set `PATH` to include `~/.local/bin`.

### Added

- `pan up`: idempotently starts the orchestrator in its hc terminal (stateless, so a fresh session
  after a reboot is correct; it drains once on boot), marks workers whose terminals are gone
  `failed`, and wakes an orchestrator that is already running. Optional `com.pan.up` LaunchAgent runs
  it at login and is retried by launchd only on failure.
- `pan reopen --thread --task`: reopens a finished or lost worker in its existing worktree in a new
  terminal under the same name, continuing that worktree's last Claude conversation.
- `pan hook session-start` and `src/pan/transcript.py` (the Stop hook's transcript parser,
  generalized to render a whole conversation).
- An opt-in integration test (`PAN_HC_INTEGRATION=1`) that drives the adapter against a private hc
  core with its own `HC_SESSION` and scratch XDG folders, using only a plain program.

- `@pan help` — instant, auto-generated command help in Slack (Task 32, Milestone M13). A phone user
  talking to pan had no way to discover what to say, and typing `help` fell through `parse_directive`
  to DELEGATE and spawned a worker briefed "help". The gateway now detects a help request and answers
  it ITSELF, before the inbox, watcher and orchestrator: a new leaf module `src/pan/help.py` provides
  pure `parse_help_request(raw_text) -> HelpRequest | None` and total
  `render_cli_help(command_path=None) -> str`, and `BoltSlackAdapter.handle_event` posts the fenced
  result through `slack_post` (the one egress, INV-4, so it runs through `to_slack_mrkdwn` — a fence
  passes through verbatim, keeping the columns) and returns. This is a DELIBERATE, DOCUMENTED, NARROW
  exception to INV-1 ("the gateway is dumb"), justified because help is read-only, spawns nothing and
  mutates no state: no inbox append, no thread-map write, no worktree, and no `:eyes:` (the instant
  reply IS the acknowledgement). Help is the only thing the gateway answers; every other event takes
  the unchanged ack-then-append path, and an unauthorized sender's `help` is still dropped by
  `auth_check` before detection. The help TEXT is generated in-process from the Typer app, so it can
  never drift from the real CLI: a fresh `typer.main.get_command(app)` per call with
  `rich_markup_mode = None` set recursively (Typer's default emits Rich boxes plus ANSI and returns an
  empty string — unusable in a Slack code block), `info_name="pan <name>"` for a named command (a bare
  name doubles the usage line to `pan pan relay`), the top-level listing filtered to the user-facing
  commands, and an unknown name degraded to a one-line notice plus the listing rather than an error.
  The `pan.cli` import is function-local, which is what breaks the
  `gateway/app.py -> help.py -> cli.py -> gateway/app.py` cycle. Detection is deterministic (INV-3)
  and phone-proof: it reuses `directive.py`'s `_LEADING_MENTION` strip and `_normalize_punctuation`,
  so `help`, `--help`, `-h`, `?`, a phone-autocorrected `—help`, and `help <command>` all resolve with
  or without the leading mention. A real task is never hijacked — more than two meaningful tokens is
  not help, and in TRAILING position only the flag forms count (`relay --help` is help; `deploy ?` and
  `retry help` are tasks and still reach the inbox). Because the Slack-only directive grammar lives in
  `parse_directive` and not in Typer, the generator cannot see it, so a short hand-written
  `SLACK_DIRECTIVE_FLAGS_FOOTER` (the `!` prefix, `--sync`, `--status`, `--sessions`, `--new`,
  `--stream <name>`, the relay/read verbs) is appended to the top-level help and labels the generated
  listing as terminal commands. The unknown-command notice echoes Slack-supplied text, so it is
  reduced to a command-name charset and truncated to a bounded length first — a backtick run would
  otherwise break out of the code fence and a `<!channel>` entity survives a fence unescaped. Four new
  named constants live in `config.py` (Principle 7); no new dependency, no new exception type (both
  functions are total), and no change to `parse_directive`, `TaskMode`, the `SlackAdapter` seam,
  `slack_post` or `to_slack_mrkdwn`. Added tests (`tests/unit/test_help.py`, `tests/unit/
  test_gateway_app.py`, parametrized): every help spelling with and without a mention and with phone
  em/en-dashes; the named-command spellings in both orders; fifteen non-help negatives including a
  task brief containing `--help`, the relay/read/sessions directives, and the two-word
  `deploy ?`/`retry help`/`--status ?` shapes; the top-level listing asserted against the pre-footer
  segment (user-facing commands present, infra commands absent, footer last); plain-text assertions
  anchored on real rendered content so they cannot pass on the empty string Rich returns; the
  `pan relay` usage line (not `pan pan relay`); a freshness test pinning a real option's help string;
  the unknown-command fallback; the sanitizer against fences, Slack entities and a 50,000-character
  name; non-mutation of the shared Typer app; and at the gateway, that a help mention posts a fenced
  body and does NOT append or react, that a backtick command name cannot break the fence, that a
  normal mention still acks and appends, and that an unauthorized help mention is still dropped. The
  live-verify that `@pan help` and `@pan help relay` render as clean monospace on real desktop and
  mobile Slack (and the R-2 column-width question) is deferred to a human session.

- Mobile smart-punctuation normalization + bare `sessions` routing in `parse_directive` (Task 31,
  Milestone M12). A phone typed `@pan --sessions`, but the keyboard autocorrected the ASCII `--`
  into an em-dash `—` glued to the word, so `—sessions` fell through the flag scan into DELEGATE and
  spawned a worker instead of showing the read-only sessions view. `parse_directive` now runs a
  leading `_normalize_punctuation(text)` pass (pure, deterministic, INV-3) on the whole stripped
  text before any verb/flag detection: an em-dash `—` (U+2014) or en-dash `–` (U+2013) GLUED to a
  word char becomes `--` (a SPACED prose dash like `plan — which` is real punctuation and is left
  intact), and curly quotes (`“ ” ‘ ’`, also autocorrected) are straightened. It also adds a bare
  leading `sessions` word as a SESSIONS trigger (position 0 only, mirroring the leading `relay`/
  `read` verbs and carrying `cleaned_text=""` like `status`). Net: `--sessions`, `—sessions`,
  `–sessions`, and bare `sessions` all deterministically route to `TaskMode.SESSIONS` — no worker
  spawned; `read sra —full` is repaired to `--full`; relay/read verbs survive normalization
  unchanged. Per R-6, normalization is whole-text in v1, so a glued em-dash inside a relay/delegate
  message body (`fix—now`) is also repaired to `fix--now` — an accepted, documented tradeoff (the
  phone-autocorrect case is the target). No new dependency, no new exception, no `Directive`/
  `TaskMode` change (reuses `TaskMode.SESSIONS`). Added tests (`tests/unit/test_directive.py`,
  parametrized): the four sessions spellings → SESSIONS (with and without a leading Slack mention);
  bare `sessions` mid-prose stays DELEGATE; `read sra --full` and `read sra —full` both → full=True;
  relay still parses after normalization; a spaced prose em-dash stays DELEGATE with prose intact and
  is preserved verbatim inside a relay message; the R-6 glued-em-dash-in-relay case is repaired; and
  curly double/single quotes are straightened with the message otherwise verbatim. The live-verify
  that `@pan —sessions` (phone em-dash) routes to the sessions view without spawning is deferred to a
  human session.
- GFM → Slack mrkdwn rendering at the single egress (Task 30, Milestone M12). pan was posting
  GitHub-flavored markdown (`**bold**`, `## Header`, `| a | b |` tables) into Slack, which renders a
  different dialect (mrkdwn) — so bold showed literal asterisks, headers showed literal `#`, and
  tables showed walls of pipes (confirmed live across three threads). A new pure
  `to_slack_mrkdwn(text: str) -> str` in `src/pan/slack_format.py` now converts GFM to Slack mrkdwn,
  adapting the placeholder-protection pipeline from Hermes' `gateway/platforms/slack.py::format_message`
  (protect fenced code / inline code / `[t](u)`→`<u|t>` links / existing Slack entities `<@U…>`/`<#…>`/
  `<url|text>` / blockquote markers → unescape-then-escape `& < >` → headers `#..######`→`*bold*`
  (inner `**` stripped) → `***x***`→`*_x_*` → `**x**`→`*x*` → single `*x*`→`_x_` with the
  both-sides-non-whitespace guard so `a * b * c` stays literal → `~~x~~`→`~x~` → restore placeholders
  in reverse order) PLUS a step Hermes lacks: a GFM table (header row + a `:?-+:?` delimiter row whose
  cell count matches the header, per the GFM rule) degrades to an aligned monospace fenced code block
  (Slack has no table primitive), each column left-padded to its widest cell. The protect/restore
  guarantee means fenced/inline code, existing Slack entities, and already-valid mrkdwn pass through
  byte-for-byte (`**` inside code is never converted). The converter is applied at the SINGLE Slack
  egress — inside `slack_post`, immediately before `SlackAdapter.post_message` — so EVERY message pan
  emits (orchestrator summaries, worker Stop/Notification-hook output, acks, relay/read replies) is
  normalized in one choke point (INV-4); the length-only log is unchanged (INV-9) and `mrkdwn=True`
  stays. The converter is a pure `str -> str` leaf module importing only stdlib `re` and three named
  `config.py` constants (`SLACK_MRKDWN_PLACEHOLDER_TEMPLATE`, `SLACK_TABLE_COLUMN_SEPARATOR`,
  `SLACK_TABLE_PAD_CHAR` — Principle 7, no inlined tunables); it imports no `slack_sdk`, opens no
  network/subprocess, and adds no dependency and no new exception (it is total: empty → empty; a regex
  defect would be a fail-fast programming error, not a modeled failure). Added tests
  (`tests/unit/test_slack_format.py`, parametrized): emphasis + headers incl. the `a * b * c`
  disambiguation guard; links + strikethrough + blockquote; protected fenced/inline code and Slack
  entities pass through untouched; `& < >` escaping with unescape-first (no double-escape) and a
  protected entity left unescaped; a genuinely 3-column table and a 2-column table → aligned fenced
  block; a pipe row without a delimiter row (and a prose-pipe line followed by a `---` divider) left
  as prose with the divider surviving; totality (empty → empty, plain prose unchanged); and
  `slack_post` runs text through the converter before `post_message` (asserted on the recorded seam
  argument) with the value-free log still firing. The live-verify that a table/bold renders cleanly
  in real Slack on desktop and mobile is deferred to a human session.
- `pan read <target> [--full]` — read ANY live herdr session's output (Task 29, Milestone M11).
  Completes the M11 interactive interface alongside `pan relay`: the command enumerates the live set
  once via `list_workspaces()`, resolves the target with the same `resolve_target` precedence
  (workspace_id > pane_id > label; `TargetNotFoundError` exit 20 / `TargetAmbiguousError` exit 21),
  and returns RAW content — recent rendered pane lines by default (fast, via a new
  `HerdrAdapter.read_pane(pane_id, lines)` shelling `herdr pane read <pane> --source recent --lines
  N`), or the full transcript with `--full` (via a new `MorcliAdapter.transcript(handle)` wrapping
  `morcli open`, reusing the M10 `resolve_session` handle logic). Because `herdr pane read` prints
  TEXT rather than the `{"result": {...}}` JSON envelope, the herdr adapter grew a raw-stdout
  `_run_text` helper (sibling of the JSON `_run`; same `HerdrError`-on-non-zero-exit handling); the
  morcli adapter grew a `_run_open` helper (`MorcliError` on failure — a read that cannot read is a
  real failure, unlike the tolerated best-effort enrichment on the sessions path). The
  `read_session(herdr, morcli, selector, sessions, *, full, lines)` core in `interface.py` selects
  the seam by `full` and touches only the one it needs; an unresolvable selector raises before either
  seam. The CLI stays deterministic and returns RAW content (BR-5 sanctioned stdout) — the SUMMARY is
  the orchestrator's job (INV-3): the new SKILL.md READ route runs `pan read`, LLM-summarizes the
  recent lines, and posts through the single egress (INV-4); `--full` posts the transcript verbatim,
  chunked to Slack's message limit. New `TaskMode.READ` and `Directive.full`; the `read <target>
  [--full]` leading-verb grammar (verb only in position 0; token 1 = target; remainder scanned only
  for `--full`; read carries no message, `cleaned_text` empty; a non-leading "please read the logs"
  stays DELEGATE). The recent-lines window is a named `READ_RECENT_LINES` constant in `config.py`
  (default 200), not an inlined literal. The read logs are value-free (pane_id / selector / full /
  handle / lengths only — INV-9; the read content is never a log argument). Added tests: `read_pane`
  (argv + verbatim TEXT via `_run_text`; non-zero exit → `HerdrError`); `transcript` (resolve+open
  returns content, fallback to the raw handle when morcli has no stream yet, open failure →
  `MorcliError`); `read_session` (recent reads the pane and leaves morcli untouched; full reads the
  transcript by workspace_id and leaves the pane read untouched; an unresolvable selector — not-found
  AND ambiguous — raises before either seam, parametrized over `full`); the read directive grammar;
  and the `pan read` CLI (recent echoes raw pane text and passes `READ_RECENT_LINES`; `--full` echoes
  the transcript by workspace_id; not-found propagates). The live-verify of the Slack `@pan read`
  summary / `--full` chunked transcript and the real `herdr pane read` text format is deferred to a
  human session.
- `pan relay <target> <message>` — drive ANY live herdr session's pane by label, pan-owned OR
  external (Task 28, Milestone M11). Makes the M10 read-only sessions view interactive: the command
  builds `ShellHerdrAdapter`, enumerates the live set once via `list_workspaces()` (the M10 seam,
  no new enumeration), resolves the target, and sends the message into its pane followed by a
  content-free nudge (reusing the existing `send_text` + `nudge` seams — no new herdr write path).
  Targeting is a new pure `resolve_target(selector, sessions)` core in a new `src/pan/interface.py`
  (the interactive drive layer, kept separate from `sessions.py`'s read-only projection): exact-match
  with precise-id precedence — a `workspace_id` match wins, then `pane_id`, then `workspace_name`
  (label), of which zero raises `TargetNotFoundError` (CLI exit 20), one resolves, and more than one
  raises `TargetAmbiguousError(selector, candidates)` (CLI exit 21), which carries the candidate
  `LiveSession`s and renders their label + workspace_id + pane_id so the user can re-target by a
  precise id (INV-3: exact string logic, no fuzzy judgment). `relay_to_session` returns the resolved
  session so the CLI/orchestrator can build a precise ack; an unresolvable selector raises before any
  send, so a bad target never drives a pane. Directives gained a leading-verb grammar: `relay
  <target> <message...>` is recognized only in position 0 (a leading `!` before it still parses), the
  second token is the `target`, and EVERYTHING after it is the `message` verbatim — no flag scan of
  the body, so a flag-looking token inside a worker brief (`add the --json path`) survives; a
  non-leading "relay" ("please relay this") stays DELEGATE. New `TaskMode.RELAY` and `Directive`
  fields `target`/`message`; the value-free relay log records only pane_id, selector, and message
  length (INV-9 — the message text is never a log argument). The orchestrating skill gained a RELAY
  route (deterministic `pan relay`, single-egress ack, exit-20/21 not-found/candidate-list handling;
  no fall back to spawn). Added tests: `resolve_target` precedence (unique label / workspace_id
  precedence / pane_id / not-found / ambiguous-with-candidates incl. the rendered re-target string);
  `relay_to_session` (sends into the resolved pane then nudges and returns the target; no send on an
  unresolvable selector); the relay directive grammar (verbatim flag-looking message, leading `!`,
  missing target, non-leading verb); and the `pan relay` CLI (echoes label+pane; not-found/ambiguous
  propagate) plus exit-code table entries 20/21. The live-verify of the Slack `@pan relay` path is
  deferred to a human session.
- `pan sessions` — a reconciled "what's running" view (Task 26, Milestone M10). It enumerates ALL
  live herdr claude sessions (a new `HerdrAdapter.list_workspaces()` shelling `herdr workspace list`
  plus a `pane list` per workspace, mapping vendor JSON to the confined `LiveSession` domain type —
  INV-8), left-joins the pan thread map to identify pan-owned sessions (by `workspace_name`, with a
  symlink-safe resolved-cwd fallback), and FLAGS status drift between pan's recorded `WorkerStatus`
  and herdr's live `AgentStatus` without healing it (report-only in v1; INV-7 stays read-only). The
  reconcile lives in a new pure `collect_sessions(herdr, thread_map, morcli)` core in
  `src/pan/sessions.py` with a directly-tested `session_drift` predicate (idle/working→running,
  blocked→blocked, done→done; `SPAWNING` and `UNKNOWN` excluded; the live-test case pan-`blocked`
  vs herdr-`idle` is drift). morcli enrichment is best-effort and tolerates `MorcliError` so a
  morcli hiccup never drops a session. New models: `AgentStatus` (StrEnum, vocabulary flagged R-9 as
  unverified against live herdr), frozen `LiveSession` and `SessionSummary`; new `ThreadMap.records()`
  read accessor. The command prints a human table or, with `--json`, the `SessionSummary` list for
  the orchestrator. Added tests: herdr `list_workspaces` argv/parse/agent-status-mapping/malformed
  and missing-cwd branches; the `collect_sessions` reconcile (pan-owned by name, resolved-cwd
  fallback, external, records-present-no-match, drift cross-product, morcli-error tolerated, no-morcli)
  and the `session_drift` matrix; and the `pan sessions` `--json` and human-table CLI paths.
- The `@pan sessions` Slack path and real morcli session linkage (Task 27, Milestone M10). Added a
  `TaskMode.SESSIONS` mode and taught `parse_directive` to route it: the explicit `--sessions` flag
  (canonical) and a fixed, deterministic soft-trigger phrase set ("what's running", "list threads",
  "list all the threads", ...) matched by pure string logic (INV-3), never model judgment. Precedence
  is deterministic — an explicit `--sessions`/`--status` flag is authoritative (`--sessions` wins
  when both appear), and the soft trigger outranks `--sync`/delegate but not an explicit `--status`.
  The orchestrating skill gained a sessions route that runs `pan sessions --json`, formats the result,
  and posts it in-thread through the single Slack egress (INV-4), touching no worker. Closed the R-7
  null-`morcli_session` gap: `spawn_worker` now takes a `morcli` adapter and best-effort captures the
  session handle at spawn via the new `MorcliAdapter.resolve_session(workspace_id) -> str | None`
  (returns None on the indexing lag, tolerates a `MorcliError` so a morcli hiccup never fails the
  already-launched worker). Added tests: the directive `--sessions`/soft-trigger/precedence cases;
  `resolve_session` returns the session id / None on no-match / raises `MorcliError` on subprocess
  failure; and `spawn_worker` captures the handle, tolerates the lag (None), and tolerates a
  `MorcliError` without failing the spawn.

  DEFERRED to a human session (mocks cannot exercise it): the live `@pan sessions` round-trip — from
  the phone, the orchestrator running `pan sessions --json` and posting the reconciled summary through
  the real Slack egress.

### Changed

- Reconciled the tech spec (`docs/superpowers/specs/2026-07-16-pan-tech-spec.md`) with what was
  actually built (Task 25, Milestone M9 — doc-only, no code changed; the spec is a local working
  artifact). Brought the contracts back in line with the implementation: added `MorcliError` to the
  exception taxonomy (now ten, mapped to exit 19 at the CLI boundary) and mirrored `errors.py`
  declaration order; added `channel: str` to the `ThreadRecord` domain model; corrected the seam and
  CLI signatures that changed during the build — `AgentLauncher.launch(worktree, pane_id, brief) ->
  None`, `ThreadMap.get_by_worktree`, `FileThreadMap(threads_path, clock)`,
  `WatchdogInboxWatcher(herdr, orchestrator_pane_id, inbox_dir)`, `status`/`stop` keyed on
  `--thread`, `spawn`/`slack-post` taking `--channel`, `threads set` requiring `--status`
  (update-only), and the new `pan watcher` and `pan hook stop|notification` commands; and recorded
  the corrected behaviors (the watcher sends a fixed `WAKE_INSTRUCTION` then Enter rather than a bare
  Enter, `ShellHerdrAdapter._run` uses `expect_json=False` for no-output commands, the thread-map
  read tolerates legacy/malformed records, and `get_by_worktree` matches on resolved paths).

### Fixed

- Completion-hook cwd match now resolves symlinks (Task 24, Milestone M9).
  `FileThreadMap.get_by_worktree` compared the worker's cwd (delivered by a Claude Code Stop/
  Notification hook) to the stored `worktree_path` by exact string equality, which silently no-ops
  when the two differ only by a symlinked prefix — on macOS the hook's cwd arrives as
  `/private/tmp/...` while the stored path was recorded as `/tmp/...`, so the hook found no record,
  posted nothing, and never marked the thread DONE/BLOCKED. The lookup now compares `Path.resolve()`
  on both sides (symmetric, so it doesn't matter which side carries the symlink), keeping the
  normalization in one place so the CLI hook boundary stays free of path handling. INV-7 (the thread
  map as the sole binding) is preserved.

### Added

- Caffeinate policy helper, launchd templates, and README (Task 23, Milestone M9) — the buildable,
  non-live parts of the original always-on Task 20:
  - `should_caffeinate(active_worker_count, on_ac_power)` in `src/pan/power.py`: a pure R-3 policy
    predicate returning True only when on AC power AND at least one worker is active (so the machine
    is never forced awake for nothing, and never on battery). The actuation that would call
    `caffeinate`/`pmset disablesleep 1` off this decision stays deferred to the live human session;
    this milestone ships only the decision, and the README says so.
  - `launchd/com.pan.gateway.plist.template` and `launchd/com.pan.watcher.plist.template`:
    LaunchAgent templates for the two always-on processes (`pan gateway`, `pan watcher`), both
    `KeepAlive=true`/`RunAtLoad=true`, with a single `<PAN_HOME>` placeholder and no secrets (tokens
    stay in `~/.pan/credentials.json` at 0600).
  - `README.md`: install (`uv tool install --editable .`), the Slack app manifest at
    `slack/manifest.yaml`, `pan config set-token`, the `~/.config/pan/config.json` shape, loading the
    two LaunchAgents, the CLI command table, the directive-flag reference, and the example first task.

- `pan watcher` CLI command (Task 22, Milestone M9). The inbox watcher had no first-class
  entrypoint (it was run via an ad-hoc script during the smoke); it now has one. `pan watcher` builds
  `ShellHerdrAdapter` + `WatchdogInboxWatcher` from config (`orchestrator.pane_id`, `paths.inbox`) and
  calls `start()` — a blocking, launchd-supervised long-runner just like `pan gateway`. The
  `WatchdogInboxWatcher.start()` observer wiring and the `_InboxEventHandler` event forwarding, both
  previously `# pragma: no cover`, are now unit-tested by mocking the `watchdog` Observer (no real
  filesystem-event timing), and the pragmas are removed.

- Worker completion hooks wired for Slack auto-reply (Task 21, Milestone M8). Post-smoke, the worker
  now replies its result to the originating thread on finish and asks its question when blocked:
  - `ThreadRecord.channel: str` — the binding now carries the Slack channel so a completion hook can
    reply without being handed a channel out of band.
  - `ThreadMap.get_by_worktree(worktree_path)` (Protocol + `FileThreadMap`) — resolves the record
    whose `worktree_path` matches, so a hook finds its thread from the worker's cwd (the thread map
    stays the single source of truth, INV-7).
  - `spawn_worker` sets `channel` on both the success and failed `ThreadRecord`, and writes the worker
    `.claude/settings.json` into the worktree (before launch) registering the Claude Code `Stop` and
    `Notification` hooks to run `pan hook stop` / `pan hook notification`. The settings schema was
    confirmed against current Claude Code docs.
  - `pan hook stop` / `pan hook notification` CLI commands — each reads the hook JSON from stdin,
    extracts `cwd`, resolves the `ThreadRecord` via `get_by_worktree`, and calls the existing
    `stop_hook` / `notification_hook` with the record's `thread_ts` + `channel` (Stop → posts the
    summary and marks DONE; Notification → posts the question and marks BLOCKED). They reuse the same
    Slack-adapter construction as `slack-post` (no Socket Mode in a hook, single egress INV-4), and a
    hook that can't resolve its thread (unknown cwd or unparseable stdin) exits cleanly without
    posting so it never crashes the worker. Live-verify of the real hook invocation is deferred to a
    human session.

- Project scaffold: `uv` + `uv_build` packaging (`pyproject.toml`, `.python-version`, `.gitignore`
  excluding `/docs/*`) with the Typer console-script entry, runtime deps (`typer`, `pydantic`,
  `slack_bolt`, `watchdog`) and dev deps (`pytest`, `respx`, `ruff`, `ty`).
- `initialise_logger` (`src/pan/logging.py`): CLI-adapted logging — file handler at DEBUG under
  `~/.pan/logs`, console handler on stderr with level from `PAN_LOG_LEVEL` (default WARNING),
  idempotent, `propagate=False`.
- `PanError` taxonomy (`src/pan/errors.py`): the base plus `UnauthorizedSenderError`,
  `ConfigMissingError`, `CredentialsError`, `InboxError`, `ThreadNotFoundError`, `SpawnError`,
  `HerdrError`, `SlackPostError`, `GatedOpDeniedError`.
- Seam Protocols (`src/pan/seams.py`): `Clock` and `IdGen` as the single import point for injectable
  seams (the remaining seam Protocols land here as their tasks arrive).
- Clock and id-generator adapters (`src/pan/adapters/clock.py`): `SystemClock` and `UuidGen`.
- Domain and config models (`src/pan/models.py`): the `TaskMode`, `WorkerStatus`, `Autonomy`, and
  `Agent` `StrEnum`s; the frozen domain models `Directive` and `InboxItem` and the mutable
  `ThreadRecord`; and the config models `SlackCredentials`, `SlackConfig`, `UserPolicy`,
  `OrchestratorConfig`, `Defaults`, `PanPaths`, and the composed `PanConfig`. Tokens are typed
  `SecretStr`, paths are `pathlib.Path`, and models are frozen wherever the value is immutable so the
  gateway, config loader, and stores can parse untrusted JSON into typed domain objects at the
  boundary.
- `parse_directive` (`src/pan/directive.py`): pure, deterministic flag parser that maps a Slack
  message to a `Directive` — `--sync` / leading `!` → SYNC, `--status` → STATUS, no mode flag →
  DELEGATE (with STATUS winning over SYNC when both appear); `--new` → `force_new`; `--stream <name>`
  → `target_stream`; `--agent <x>` parsed but reserved for v2. `cleaned_text` strips every recognized
  flag (including a dangling value-flag and a value-flag whose value is another flag) while preserving
  the task prose. No I/O and no model judgment, so `TaskMode` is deterministic per INV-3.
- `load_config` (`src/pan/config.py`): the single config composition root. Reads
  `~/.config/pan/config.json` (or an explicit path), parses the JSON at the boundary into a
  `PanConfig`, expands `~` in the path-bearing fields (`paths.*` and `orchestrator.worktree_base`),
  and falls back to model defaults for omitted optional sections. A missing file, non-object or
  malformed JSON, or a missing required field is translated to `ConfigMissingError`; error messages
  carry only the path, never file contents. `PanConfig` holds no secrets (tokens live in
  `credentials.json`), so nothing here calls `.get_secret_value()`.
- Credentials store (`src/pan/credentials.py`): `save_credentials` / `load_credentials` over
  `~/.pan/credentials.json` (the gccli-style 0600 file, not the OS keychain — the deliberate,
  BR-3-enforced deviation for Linux-VPS portability). `save_credentials` is the sole sanctioned
  `.get_secret_value()` call site outside Slack-client construction; it writes the token payload to a
  fresh `O_EXCL` temp file created at 0600 and atomically `os.replace`s it into place, so a secret
  never touches disk at looser-than-0600 permissions even when overwriting a pre-existing loose file.
  `load_credentials` parses the JSON at the boundary into `SlackCredentials` (`SecretStr` fields),
  raises `CredentialsError` on a missing or malformed file, and warns (via the named logger, mode and
  path only — never a token) when the file is group/other-readable. Tokens stay `SecretStr` end to
  end and mask as `**********` in repr/str.
- `FileInboxStore` (`src/pan/inbox.py`), implementing the new `InboxStore` seam Protocol (added to
  `src/pan/seams.py`): a file-per-event on-disk inbox over `~/.pan/inbox/`. `append` writes each event
  to `{event_id}.json` via a temp-file + atomic replace, using the Slack event id as the filename so a
  redelivered event before a drain resolves to one file and is drained exactly once (INV-6); it rejects
  an event id that is not a safe single path segment (no `/`, `\`, `..`, NUL) so a hostile id cannot
  escape the inbox directory. `drain` claims each entry with an atomic rename (so a concurrent gateway
  append or a second drainer cannot lose or double-return an event — R-6), returns the items ordered by
  `received_at`, and empties the store. A malformed entry is quarantined to `.corrupt` and surfaced as
  `InboxError`, while cleanly-parsed siblings are restored for a later drain rather than lost. Logs are
  value-free (id/channel/count only, never `raw_text`).
- `FileThreadMap` (`src/pan/threadmap.py`), implementing the new `ThreadMap` seam Protocol (added to
  `src/pan/seams.py`): the single source of truth for the thread-to-worker binding (INV-7), a JSON
  object at `~/.pan/threads.json` keyed by `thread_ts`. `put` upserts a `ThreadRecord`, `get` returns
  the record or `None`, and `update_status` transitions the worker status and bumps `updated_at` from
  an injected `Clock` seam (so the timestamp is deterministic in tests), preserving every other field;
  it raises `ThreadNotFoundError` when the thread is unknown. Writes are atomic (temp-file + replace)
  and `Path`/enum/`datetime` fields survive the disk round-trip. Logs are value-free (thread/status).
- `ShellHerdrAdapter` (`src/pan/adapters/herdr.py`), implementing the new `HerdrAdapter` seam Protocol
  (added to `src/pan/seams.py`): shells the real `herdr` CLI over its JSON socket-API helpers, the only
  module permitted to invoke `herdr`/`subprocess` (BR-2). `create_workspace` runs `herdr workspace
  create --cwd <path> --label <label> --no-focus` and, because that command returns only workspace
  metadata (no pane id), resolves the created pane with a follow-up `herdr pane list --workspace <id>`
  (selecting the pane in the workspace's active tab, else the first pane), returning `(workspace_id,
  pane_id)`. `nudge` issues the fixed, content-free `herdr pane send-keys <pane> Enter` (no payload
  crosses the pane — INV-2); `send_text`/`kill_pane` map to `pane send-text` / `pane close`. All herdr
  JSON is parsed inside the adapter and only plain `pan` domain values cross the boundary (INV-8);
  subprocess failures, non-JSON output, and OS errors become `HerdrError`. Logs are value-free (pane/
  workspace ids and text length only — never the message body). The exact nudge keystroke and JSON key
  names are flagged for live reconfirmation (R-2).
- `ShellGitWorktreeAdapter` (`src/pan/adapters/git_worktree.py`), implementing the new
  `GitWorktreeAdapter` seam Protocol: the only module permitted to shell `git` (BR-2).
  `create_worktree(repo, branch, base)` runs `git -C <repo> worktree add <base>/<branch> -b <branch>`
  and returns the worktree `Path`, rejecting (`SpawnError`) a branch whose target would escape `base`
  (traversal guard via `is_relative_to`). `remove_worktree(path)` runs `git -C <path> worktree remove
  --force <path>` — teardown forces removal because a worker leaves its worktree dirty, and its result
  is already captured by then. Git failures (non-zero exit with stderr detail, or a missing git
  binary) translate to `SpawnError`; only a `pathlib.Path` crosses the boundary (INV-8). Subprocess is
  a list argv (no shell injection). Logs are value-free.
- `ShellMorcliAdapter` (`src/pan/adapters/morcli.py`), implementing the new `MorcliAdapter` seam
  Protocol: the only module permitted to shell `morcli` (BR-2). `session_status(handle)` runs
  `morcli streams --json`, finds the stream whose `session_id` or `workspace_id` matches the handle
  (absorbing the open R-7 handle-binding ambiguity), and maps the agent status to a `WorkerStatus`
  (`working`/`idle` → RUNNING, `blocked` → BLOCKED, `done` → DONE, `unknown` → FAILED). An
  unrecognized status, an unknown handle, non-JSON/non-list output, a non-zero exit, or a missing
  `morcli` binary all raise `MorcliError`; only a `WorkerStatus` crosses the boundary (INV-8).
- New `MorcliError` (`src/pan/errors.py`): a `PanError` subclass for morcli subprocess failures. This
  extends the tech-spec's original nine-item taxonomy to ten — no existing error fit (reusing
  `HerdrError`, whose meaning is "herdr subprocess failed", would be a misnomer), and the morcli
  adapter genuinely introduces this failure mode.
- `auth_check` (`src/pan/gateway/auth.py`): the gateway's safety gate 2 — pure allowlist + channel
  policy enforced before any work. Returns `None` when the sender is in the `users` mapping and the
  channel is permitted (the `"*"` wildcard means all channels, or the channel is explicitly listed);
  raises `UnauthorizedSenderError` otherwise. Fail-closed: an unknown sender, an empty `users` map,
  and an explicitly empty `channels` list all deny. Pure logic — no Slack SDK import — and the
  denial is logged value-free (channel only, no sender payload).
- `BoltSlackAdapter` (`src/pan/gateway/app.py`), implementing the new `SlackAdapter` seam Protocol:
  the always-on Bolt Socket Mode gateway and the sole importer of `slack_sdk`/`slack_bolt` (BR-1) and
  the single Slack-client construction point (`.get_secret_value()` for the bot token in `__init__`
  and the app token in `start()` — BR-3). Its `handle_event` parses an untrusted Slack event dict into
  a typed `InboxItem` at the boundary, auth-checks the sender, adds a fast `:eyes:` reaction to the
  message ts BEFORE appending (INV-1 ack ordering), and appends to the `InboxStore` — it never spawns,
  classifies, or touches the thread map (stateless gateway, INV-1). A denied sender is dropped with no
  reaction and no append. A top-level app-mention roots its thread at the message ts
  (`is_thread_reply=False`); a reply carries the parent `thread_ts`. `add_reaction`/`post_message`
  translate `SlackApiError` to `SlackPostError`. `start()` (live Socket Mode, not unit-tested) wires
  the app-mention and message handlers, using the pure `_should_forward_message` filter to forward
  only human thread replies that don't mention the bot — avoiding the double-append when Slack
  delivers an in-thread mention as both an `app_mention` and a `message` event.
- `slack_post` (`src/pan/gateway/slack_post.py`): the single Slack egress path (INV-4). Every
  worker→thread post routes through this one function, which logs value-free (thread ts + text
  length, never the body) and delegates to `SlackAdapter.post_message`; an adapter failure surfaces
  as `SlackPostError`. Depends on the `SlackAdapter` Protocol, not a concrete — no Slack SDK import.
- `spawn_worker` + `ClaudeLauncher` (`src/pan/spawn.py`), adding the `AgentLauncher` seam Protocol:
  the worker-spawn orchestration. `spawn_worker` derives the stream label (`pan-<stream>`, or
  `pan-<short-id>` when no stream is given) and runs create_worktree → create_workspace → launch →
  `ThreadMap.put` (the SPAWNING record — the sole thread→worker binding, INV-7) → ack via `slack_post`
  ("on it — stream pan-…", INV-4). A `SpawnError`/`HerdrError` from any step records a FAILED
  `ThreadRecord`, posts a spawn-failed notice through the single egress path, and re-raises
  `SpawnError`. `ClaudeLauncher.launch` starts the worker by sending `claude <shell-quoted brief>`
  into the pane (herdr `send_text`) and submitting it with the fixed Enter nudge; the brief is
  `shlex.quote`d so arbitrary task text can never run as a shell command. The `AgentLauncher` seam is
  `launch(worktree, pane_id, brief) -> None` — a deliberate refinement of the plan's illustrative
  `launch(…, workspace, …) -> ThreadRecord`, since the launcher needs the pane id (not the workspace
  id) to send the brief and the orchestrator owns record construction (the record's required
  thread_ts/channel are not available to the launcher). Spawn logs are value-free (label + thread ts).
- `WatchdogInboxWatcher` (`src/pan/watcher.py`), implementing the final seam Protocol `InboxWatcher`:
  the filesystem watcher that wakes the orchestrator. `on_inbox_changed` issues exactly one fixed,
  content-free nudge to the orchestrator pane via `HerdrAdapter.nudge` — no payload ever crosses the
  pane (INV-2); the request travels through the durable inbox. `start()` (live observer thread, not
  unit-tested) wires a `watchdog` observer over the inbox dir whose callback invokes
  `on_inbox_changed`. Value-free logs.
- Completion hooks (`src/pan/hooks/stop.py`, `src/pan/hooks/notification.py`): the reply layer that
  runs when a worker finishes or needs input. Each parses the Claude Code hook JSON from stdin into a
  frozen pydantic boundary model, resolves the `ThreadRecord` (raising `ThreadNotFoundError` — and
  posting/transitioning nothing — when the thread is unknown), posts to the thread through the single
  `slack_post` egress (INV-4), and transitions the worker status. `stop_hook` posts the final summary
  (the last assistant text block extracted from the transcript JSONL, else a default) and marks
  `DONE`; `notification_hook` posts the Notification `message` (the question, else a default) and
  marks `BLOCKED`. The thread routing (`thread_ts`/`channel`) is injected — baked into the hook
  command at spawn — since the Claude Code payloads carry no thread context; `stdin` is an injectable
  seam for testing. Logs are value-free (thread + status). The transcript shape and the spawn-time
  correlation are flagged for live reconfirmation.
- Dormant gated-ops gate (`src/pan/hooks/pretooluse_gate.py`): safety gate 4, built but inert in v1.
  `pretooluse_gate` parses the Claude Code PreToolUse hook JSON into a frozen boundary model, matches
  the pending command against `PanConfig.gated_ops`, and — with `gated_ops` empty (the v1 default) —
  emits an allow decision and touches no Slack, so populating `gated_ops` later activates the gate
  with no code change. On a match it posts an approval request through the single `slack_post` egress
  and blocks on the decision (the inbox round-trip in production, an injected callable in tests):
  approve emits an allow decision; deny emits a deny decision to stdout (the sanctioned BR-5
  exception, via `stdout.write` + flush — never `print`) and raises `GatedOpDeniedError`. The gated-op
  log is value-free (the matched config pattern + the allow/deny bool, never the full command). The
  decision-JSON format and the required fail-closed behavior on approval-infra failure are still open
  for when the gate is activated.
- Typer CLI toolbelt (`src/pan/cli.py`, `src/pan/__main__.py`): the `pan` console entry with sub-apps
  `config`/`inbox`/`threads` and commands `gateway`, `config set-token`, `config show`, `inbox drain
  --json`, `spawn`, `threads get`, `threads set`, `slack-post`, `status`, `stop`, `pause`. Each command
  is a thin boundary that loads `PanConfig`, constructs the adapters it needs, and calls the pure core.
  `_run()` is the single error boundary: it maps each `PanError` subclass to a documented exit code
  (10–19, the base → 1) via `_exit_code_for`, translates the vendored click exceptions, and lets
  unclassified exceptions surface as tracebacks (Principle 4); `main()` = `SystemExit(_run())`. `config
  show` masks credentials (SecretStr serialization — never `.get_secret_value()`, R-4). Deliberate
  refinements over the plan's sketch: `spawn`/`slack-post` take `--channel` (channel isn't on
  `ThreadRecord`), `spawn` takes `--repo` (default cwd), and `status`/`stop` key on `--thread`
  (the thread map is keyed by `thread_ts`) with `status` resolving the morcli handle from the record.
- Orchestrating skill (`src/pan/skills/orchestrating/SKILL.md`): the prose the persistent
  `pan-orchestrator` session runs on each inbox nudge — a drain → read-directive → look-up-binding →
  route loop. It shells `pan inbox drain --json` (which now attaches the deterministically-parsed
  `directive` block per item, so mode is fixed in code per INV-3 — see below), then routes on a
  precedence ladder: STATUS first (answer from `pan status`, or a "no active worker" note; never relay
  an empty status into a pane), then a live-worker relay, then a fresh spawn for a finished/absent
  thread or `force_new`. The skill states explicitly that the model makes only the fuzzy calls
  (reuse-vs-spawn, repo, task comprehension) and every mechanical step is a `pan` subcommand.
- `pan inbox drain --json` now runs `parse_directive` in code and emits `{"item", "directive"}` per
  entry, so the orchestrator reads the mode/`cleaned_text` deterministically rather than re-deriving
  them by judgment (enforces INV-3; the flag table stays the single source of truth in `directive.py`).
