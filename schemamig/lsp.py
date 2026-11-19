"""Language server for the journal workflow (Neovim, VS Code, any LSP client).

Features, on a project's *working schema* files only:
  * diagnostics: every validation-relevant edit the journal does not explain is a warning at
    the place you made it; an open change scope shows as an info diagnostic with what it
    would record
  * code actions: "Record as <class>" for unrecorded edits (only classes that verifiably
    explain the edit are offered, via a dry run), "Begin <class>" on the property under the
    cursor, and Commit / Abort / Status / Show for the open change
  * commands (workspace/executeCommand): schemamig.begin, .record, .commit, .abort,
    .status, .list, .show
  * notification `schemamig/status` after every update, for statuslines

Arguments a client should prompt for (reason) are flagged with "prompt_reason": true.
The logic lives in `Engine` (protocol-free, unit-tested); the pygls layer is thin.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import journal as J
from . import jsonpos
from . import ops as O
from . import pointer as P
from . import schemadiff as D
from .project import Kind, Project
from .schema import SchemaError

ERROR, WARNING, INFO, HINT = 1, 2, 3, 4


def uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def _rng(span: jsonpos.Span, prefer_key: bool = True) -> dict:
    (sl, sc), (el, ec) = span.key if (prefer_key and span.key) else (span.start, span.end)
    return {"start": {"line": sl, "character": sc}, "end": {"line": el, "character": ec}}


def target_of(ptr: str) -> tuple[str, str | None]:
    """Pointer -> (owner target, field name) using the last 'properties' segment."""
    toks = P.split(ptr)
    idxs = [i for i, t in enumerate(toks[:-1]) if t == "properties"]
    if not idxs:
        from .schema import ALL_KEYWORDS
        return _as_target(toks[:-1] if toks and toks[-1] in ALL_KEYWORDS else toks), None
    i = idxs[-1]
    return _as_target(toks[:i]), toks[i + 1]


def _as_target(toks: list[str]) -> str:
    if not toks:
        return "$"
    if len(toks) == 2 and toks[0] == "$defs":
        return f"def:{toks[1]}"
    return P.join(*toks)


class Engine:
    def context(self, path: Path) -> tuple[Project, Kind] | None:
        try:
            project = Project.find(path.parent)
        except O.DslError:
            return None
        for k in project.kinds.values():
            if k.schema_file and k.schema_file.resolve() == path.resolve():
                return project, k
        return None

    # -- diagnostics ------------------------------------------------------------
    def diagnostics(self, path: Path, text: str) -> list[dict]:
        ctx = self.context(path)
        if not ctx:
            return []
        project, kind = ctx
        try:
            work = json.loads(text)
            idx = jsonpos.index(text)
        except (json.JSONDecodeError, jsonpos.JsonPosError) as e:
            line, col = (e.lineno - 1, e.colno - 1) if isinstance(e, json.JSONDecodeError) else (0, 0)
            return [self._diag(line, col, f"invalid JSON: {e}", ERROR)]
        out: list[dict] = []
        try:
            session = J.load_session(project)
            session = session if session and session["kind"] == kind.name else None
            eff = J.effective(J.read_records(kind))
            expected, _ = J.replay(kind, [e for e in eff if not session or e["id"] != session.get("amends")])
        except (O.DslError, SchemaError) as e:
            return [self._diag(0, 0, f"schemamig: {e}", ERROR)]

        items = D.residual_items(expected, work)
        st = J.status(project, work) if session else {"open": False}
        if session:
            span = jsonpos.locate(idx, self._target_ptr(expected, session["target"]))
            if st["ready"]:
                what = "; ".join(O.parse_op(dict(o), kind.root).describe() for o in st["ops"])
                msg = f"change open: {session['class']} {session['target']}: ready to commit: {what}"
            else:
                msg = f"change open: {session['class']} {session['target']}: {st['problem']}"
            out.append({"range": _rng(span), "severity": INFO, "source": "schemamig", "message": msg,
                        "code": "open-change"})
        if not (session and st.get("ready")):
            for ptr, msg in items:
                span = jsonpos.locate(idx, ptr)
                out.append({"range": _rng(span), "severity": WARNING if not session else INFO,
                            "source": "schemamig", "code": "unrecorded",
                            "message": f"unrecorded schema change at {ptr}: {msg}", "data": {"ptr": ptr}})
        errs, _ = J.check(project, kind, work)
        for e in errs:
            if "not explained by the journal" in e:
                continue
            out.append(self._diag(0, 0, f"schemamig check: {e}", ERROR))
        return out

    @staticmethod
    def _diag(line: int, col: int, msg: str, sev: int) -> dict:
        return {"range": {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + 1}},
                "severity": sev, "source": "schemamig", "message": msg}

    @staticmethod
    def _target_ptr(schema: dict, target: str) -> str:
        try:
            from .schema import resolve_location
            return resolve_location(schema, target)
        except Exception:
            return "#"

    # -- code actions ---------------------------------------------------------------
    def code_actions(self, path: Path, text: str, line: int, col: int) -> list[dict]:
        ctx = self.context(path)
        if not ctx:
            return []
        project, kind = ctx
        try:
            work = json.loads(text)
            idx = jsonpos.index(text)
        except (json.JSONDecodeError, jsonpos.JsonPosError):
            return []
        session = J.load_session(project)
        if session:
            st = J.status(project, work)
            acts = []
            if st.get("ready"):
                acts.append(self._act(f"schemamig: commit {session['class']} {session['target']}", "schemamig.commit",
                                      {"prompt_reason": not session.get("reason")}))
            acts += [self._act("schemamig: show what the open change would record", "schemamig.status", {}),
                     self._act(f"schemamig: abort {session['class']} {session['target']}", "schemamig.abort", {})]
            return acts

        acts: list[dict] = []
        eff = J.effective(J.read_records(kind))
        expected, _ = J.replay(kind, eff)
        items = D.residual_items(expected, work)
        here = jsonpos.at_position(idx, line, col)
        near = [p for p, _ in items]
        # 1. record: candidate (class, target, opts) that verifiably explain the unrecorded edits
        tried = set()
        for cls, target, opts in self._candidates(near):
            key = (cls, target, json.dumps(opts, sort_keys=True))
            if key in tried:
                continue
            tried.add(key)
            try:
                ops = J.plan(kind, cls, target, opts, work)
            except (O.DslError, SchemaError):
                continue
            what = "; ".join(O.parse_op(dict(o), kind.root).describe() for o in ops)
            acts.append(self._act(f"schemamig: record as {cls}: {what}", "schemamig.record",
                                  {"class": cls, "target": target, "options": opts, "prompt_reason": True},
                                  kind="quickfix"))
        if items:
            acts.append(self._act("schemamig: record as unclassified (raw schema patch)", "schemamig.record",
                                  {"class": "unclassified", "target": "-", "options": {}, "prompt_reason": True},
                                  kind="quickfix"))
        # 2. begin: on the property under the cursor
        owner, field = target_of(here)
        if field:
            for cls in ("rename-field", "remove-field", "set-field-schema", "make-required", "make-optional"):
                opts = {} if cls == "rename-field" else {"name": field}
                if cls == "rename-field":
                    opts = {"from": field}
                acts.append(self._act(f"schemamig: begin {cls} {owner} ({field})", "schemamig.begin",
                                      {"class": cls, "target": owner, "options": opts}, kind="refactor"))
            if P.split(here)[-1:] in (["enum"], ["const"]) or (P.split(here)[-2:-1] == ["enum"]):
                acts.append(self._act(f"schemamig: begin rename-value {owner} ({field})", "schemamig.begin",
                                      {"class": "rename-value", "target": owner, "options": {"name": field}},
                                      kind="refactor"))
        acts.append(self._act(f"schemamig: begin add-field {owner if field else _as_target(P.split(here))}",
                              "schemamig.begin", {"class": "add-field", "target": owner if field else
                                                  _as_target(P.split(here)), "options": {}}, kind="refactor"))
        return acts

    @staticmethod
    def _candidates(ptrs: list[str]):
        classes = ["rename-field", "add-field", "remove-field", "set-field-schema", "make-required",
                   "make-optional", "nest-fields"]
        for ptr in ptrs:
            toks = P.split(ptr)
            owner, field = target_of(ptr)
            if toks and toks[-1] in ("required",):
                owner = _as_target(toks[:-1])
            for cls in classes:
                yield cls, owner, {}
            if field and ("enum" in toks or "const" in toks):
                yield "rename-value", owner, {"name": field}
            for i, t in enumerate(toks):
                if t in ("oneOf", "anyOf"):
                    yield "add-variant", _as_target(toks[:i]), {}
            if len(toks) <= 1:
                yield "wrap", "$", {}

    @staticmethod
    def _act(title: str, command: str, args: dict, kind: str = "source") -> dict:
        return {"title": title, "kind": kind, "command": command, "arguments": [args]}

    # -- commands -------------------------------------------------------------------
    def execute(self, root_hint: Path, command: str, args: dict) -> dict:
        try:
            project = Project.find(root_hint)
        except O.DslError as e:
            return {"error": str(e)}
        try:
            name = command.split(".", 1)[1]
            kind = project.kind(args.get("kind")) if args.get("kind") or len(project.kinds) == 1 else None
            if kind is None and name in ("begin", "record", "list", "show"):
                kind = self._kind_for(project, args)
            if name in ("begin", "record"):
                J.begin(project, kind, args["class"], args["target"], args.get("options") or {},
                        args.get("reason") or "", args.get("compatibility"))
                if name == "begin":
                    return {"ok": True, "status": J.status(project)}
                try:
                    return {"ok": True, "record": J.commit(project)}
                except Exception:
                    J.abort(project)
                    raise
            if name == "commit":
                return {"ok": True, "record": J.commit(project, args.get("reason"), args.get("compatibility"))}
            if name == "abort":
                return {"ok": True, "aborted": J.abort(project)}
            if name == "status":
                return J.status(project)
            if name == "list":
                return {"changes": J.effective(J.read_records(kind))}
            if name == "show":
                from . import audit
                r = audit.show(project, kind, args["id"])
                return {**r, "text": audit.render(r)}
            return {"error": f"unknown command {command}"}
        except (O.DslError, SchemaError, KeyError) as e:
            return {"error": str(e)}

    @staticmethod
    def _kind_for(project: Project, args: dict) -> Kind:
        path = args.get("path")
        if path:
            for k in project.kinds.values():
                if k.schema_file and k.schema_file.resolve() == Path(path).resolve():
                    return k
        raise O.DslError("cannot tell which document kind; pass 'kind' or 'path'")


def serve() -> None:  # pragma: no cover - exercised by the headless Neovim smoke test
    from lsprotocol import types as T
    from pygls.server import LanguageServer

    engine = Engine()
    ls = LanguageServer("schemamig", "0.0.1")
    open_docs: set[str] = set()

    def publish(uri: str) -> None:
        doc = ls.workspace.get_text_document(uri)
        diags = []
        for d in engine.diagnostics(uri_to_path(uri), doc.source):
            r = d["range"]
            diags.append(T.Diagnostic(
                range=T.Range(T.Position(**r["start"]), T.Position(**r["end"])), message=d["message"],
                severity=T.DiagnosticSeverity(d["severity"]), source=d["source"], code=d.get("code"),
                data=d.get("data")))
        ls.publish_diagnostics(uri, diags)
        ctx = engine.context(uri_to_path(uri))
        if ctx:
            try:
                ls.send_notification("schemamig/status", J.status(ctx[0], json.loads(doc.source)))
            except Exception:
                pass

    @ls.feature(T.TEXT_DOCUMENT_DID_OPEN)
    def did_open(ls, params: T.DidOpenTextDocumentParams):
        open_docs.add(params.text_document.uri)
        publish(params.text_document.uri)

    @ls.feature(T.TEXT_DOCUMENT_DID_CHANGE)
    def did_change(ls, params: T.DidChangeTextDocumentParams):
        publish(params.text_document.uri)

    @ls.feature(T.TEXT_DOCUMENT_DID_SAVE)
    def did_save(ls, params: T.DidSaveTextDocumentParams):
        publish(params.text_document.uri)

    @ls.feature(T.TEXT_DOCUMENT_DID_CLOSE)
    def did_close(ls, params: T.DidCloseTextDocumentParams):
        open_docs.discard(params.text_document.uri)

    @ls.feature(T.TEXT_DOCUMENT_CODE_ACTION,
                T.CodeActionOptions(code_action_kinds=[T.CodeActionKind.QuickFix, T.CodeActionKind.Refactor,
                                                       T.CodeActionKind.Source]))
    def code_action(ls, params: T.CodeActionParams):
        uri = params.text_document.uri
        doc = ls.workspace.get_text_document(uri)
        pos = params.range.start
        out = []
        for a in engine.code_actions(uri_to_path(uri), doc.source, pos.line, pos.character):
            args = {**a["arguments"][0], "path": str(uri_to_path(uri))}
            out.append(T.CodeAction(title=a["title"], kind=T.CodeActionKind(a["kind"]) if a["kind"] != "source"
                                    else T.CodeActionKind.Source,
                                    command=T.Command(a["title"], a["command"], [args])))
        return out

    def make_cmd(name: str):
        def run(ls, args):
            a = args[0] if args else {}
            if hasattr(a, "_asdict"):
                a = a._asdict()
            a = json.loads(json.dumps(a, default=lambda o: getattr(o, "__dict__", str(o))))
            hint = Path(a.get("path")).parent if a.get("path") else Path(ls.workspace.root_path or ".")
            if name in ("schemamig.record", "schemamig.commit"):
                dirty = [str(uri_to_path(u)) for u in open_docs if engine.context(uri_to_path(u))
                         and ls.workspace.get_text_document(u).source != uri_to_path(u).read_text()]
                if dirty:
                    res = {"error": f"save the working schema first (unsaved: {', '.join(dirty)}); "
                                    "the journal must not get ahead of the file"}
                    ls.show_message(f"schemamig: {res['error']}", T.MessageType.Error)
                    return res
            res = engine.execute(hint, name, a)
            for uri in list(open_docs):
                publish(uri)
            if res.get("error"):
                ls.show_message(f"schemamig: {res['error']}", T.MessageType.Error)
            return res
        return run

    for name in ("begin", "record", "commit", "abort", "status", "list", "show"):
        ls.command(f"schemamig.{name}")(make_cmd(f"schemamig.{name}"))

    ls.start_io()
