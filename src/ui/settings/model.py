"""YOUR DOTS › AI Models: the model the Dots chat with, its provider and API keys, and the
models a voice call uses.

Only servers with ``/api/model-config`` (set up with the Dotdeck guide) let Dotdeck
change the model; on others the page says where it is set. Servers from Dotdeck 0.3.3
on also report the provider, one key per provider, prices and the three call models;
with older ones the page shows what they report and nothing more (core/models.py).
Keys are write-only: the server says whether one is configured and Dotdeck never shows
or keeps it.
"""

from __future__ import annotations

import json

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ... import config
from ...core import models
from ...i18n import _
from .common import SettingsPage, confirm, escape, status_icon, string_model, suffix_label
from .dots import ProblemGroup, server_problem

# Where OpenDots explains its .env (OPENAI_MODEL, OPENAI_API_KEY).
GUIDE = f"{config.OPENDOTS_URL}#get-started"
# Kept for code that imported them from here.
PROVIDERS = models.PROVIDER_NAMES
provider_name = models.provider_name


class Choice(GObject.Object):
    """One entry of the Model menu: a preset, the model in use, or "Custom…" (empty id)."""

    __gtype_name__ = "DotdeckModelChoice"

    name = GObject.Property(type=str, default="")
    detail = GObject.Property(type=str, default="")

    def __init__(self, model_id: str, name: str, detail: str = ""):
        super().__init__(name=name, detail=detail)
        self.model_id = model_id


def _setup_choice(_factory, item: Gtk.ListItem) -> None:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=3,
                  margin_bottom=3)
    title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
    detail = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
    detail.add_css_class("dim-label")
    detail.add_css_class("caption")
    box.append(title)
    box.append(detail)
    item.set_child(box)


def _bind_choice(_factory, item: Gtk.ListItem) -> None:
    choice = item.get_item()
    box = item.get_child()
    title = box.get_first_child()
    detail = title.get_next_sibling()
    title.set_label(choice.props.name)
    detail.set_label(choice.props.detail)
    detail.set_visible(bool(choice.props.detail))


class ModelPage(SettingsPage):
    name = "model"
    title = _("AI Models")
    icon = "applications-science-symbolic"

    def build(self) -> None:
        self._signature = None
        self._choices: list[str] = []
        self._providers: list[str] = []
        self._provider = ""            # the provider picked under Change
        self._syncing = False
        self._busy = False
        self._testing = False
        self._tested_for: tuple | None = None
        self._key_rows: list[Gtk.Widget] = []

        top = self.group()
        self.selector = self.add(top, self.win.server_selector(), ("server",))
        top.set_visible(self.selector.get_visible())
        self.selector.connect("notify::visible",
                              lambda row, _p: top.set_visible(row.get_visible()))
        self.problem = ProblemGroup()
        self.widget.add(self.problem)

        # -- the chat model in use -------------------------------------------------------
        self.current = self.group(
            _("Chat Model"),
            _("Every Dot uses it in messages (Dotdeck, the browser, Telegram), in scheduled "
              "tasks and for research during calls."))
        self.model_row = Adw.ActionRow(title=_("Model"), subtitle_selectable=True)
        self.add(self.current, self.model_row, ("llm", "model", "brain", "ai", "chat",
                                                "provider"))
        self.price_row = Adw.ActionRow(title=_("Approximate Cost"), subtitle_lines=2,
                                       visible=False)
        self.add(self.current, self.price_row, ("price", "cost", "dollars", "tokens"))
        self.key_status = Adw.ActionRow(title=_("API Key"))
        self.key_icon = status_icon("dialog-warning-symbolic")
        self.key_status.add_prefix(self.key_icon)
        self.add(self.current, self.key_status, ("key", "configured", "api key"))
        self.test_row = Adw.ActionRow(title=_("Test the Model"), subtitle_lines=3,
                                      visible=False)
        self.test_icon = status_icon("network-transmit-receive-symbolic")
        self.test_row.add_prefix(self.test_icon)
        self.test_button = Gtk.Button(label=_("Test Now"), valign=Gtk.Align.CENTER)
        self.test_button.connect("clicked", lambda _b: self.run_test())
        self.test_row.add_suffix(self.test_button)
        self.add(self.current, self.test_row, ("test", "latency", "speed", "check", "answer"))

        # -- change provider and model ---------------------------------------------------
        self.change = self.group(
            _("Change"),
            _("Every Dot on the server uses it from its next message. The server keeps the "
              "choice in its .env file."))
        self.provider_combo = Adw.ComboRow(title=_("Provider"), visible=False)
        self.provider_combo.connect("notify::selected", lambda *_: self._on_provider())
        self.add(self.change, self.provider_combo, ("provider", "openrouter", "anthropic",
                                                    "claude", "openai"))
        self.combo = Adw.ComboRow(title=_("Model"), enable_search=True,
                                  expression=Gtk.PropertyExpression.new(Choice, None, "name"))
        if hasattr(Adw, "ComboRowSearchMatchMode"):
            self.combo.set_search_match_mode(Adw.ComboRowSearchMatchMode.SUBSTRING)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", _setup_choice)
        factory.connect("bind", _bind_choice)
        self.combo.set_list_factory(factory)
        self.combo.connect("notify::selected", lambda *_: self._on_choice())
        self.add(self.change, self.combo, ("preset", "deepseek", "claude", "haiku", "sonnet",
                                           "opus", "gemini", "gpt", "price"))
        self.custom = Adw.EntryRow(title=_("Model ID, for example vendor/model-name"),
                                   visible=False)
        self.custom.connect("changed", lambda _r: self._sync_apply())
        self.custom.connect("entry-activated", lambda _r: self._confirm())
        self.add(self.change, self.custom, ("custom", "model id"))
        self.advanced = Adw.ExpanderRow(title=_("Advanced"), visible=False)
        self.base_url = Adw.EntryRow(title=_("API Address"))
        self.base_url.connect("changed", lambda _r: self._sync_apply())
        self.advanced.add_row(self.base_url)
        self.add(self.change, self.advanced, ("advanced", "base url", "endpoint", "api url",
                                              "address"))
        self.new_key = Adw.PasswordEntryRow(title=_("API Key"), visible=False)
        self.new_key.connect("changed", lambda _r: self._sync_apply())
        self.new_key.connect("entry-activated", lambda _r: self._confirm())
        self.add(self.change, self.new_key, ("key", "api key", "anthropic"))
        self.apply = Adw.ButtonRow(title=_("Switch Model…"), visible=False)
        self.apply.add_css_class("suggested-action")
        self.apply.connect("activated", lambda _r: self._confirm())
        self.add(self.change, self.apply, ("apply", "switch", "change"))

        # -- one key per provider --------------------------------------------------------
        self.key = self.group(_("API Keys"),
                              _("Type a new key to replace the one on the server. Dotdeck "
                                "never shows it and does not keep it."))
        self.key_row = Adw.PasswordEntryRow(title=_("New API Key"), show_apply_button=True)
        self.key_row.connect("apply", self._on_key)
        self.add(self.key, self.key_row, ("key", "token", "secret", "api key"))

        # -- the models of a voice call (changed on the Voice Calls page) ------------------
        self.calls = self.group(
            _("Voice Call Model"),
            _("A call uses three models: one speaks, one writes down what you say, and the "
              "chat model does the research."))
        self.speech_row = Adw.ActionRow(title=_("Speech"), subtitle_selectable=True)
        self.add(self.calls, self.speech_row, ("voice", "audio", "realtime", "gateway", "call"))
        self.transcribe_row = Adw.ActionRow(title=_("Transcription"), subtitle_selectable=True)
        self.add(self.calls, self.transcribe_row, ("transcription", "speech to text", "call"))
        self.thinking_row = Adw.ActionRow(title=_("Research During Calls"),
                                          subtitle_selectable=True)
        self.add(self.calls, self.thinking_row, ("research", "ask_compute", "call", "thinking"))
        self.calls_button = Adw.ButtonRow(title=_("Set Up Calls…"))
        self.calls_button.connect("activated", lambda _r: self.win.show_page("voice"))
        self.add(self.calls, self.calls_button, ("voice", "calls", "configure"))
        self.refresh()

    # -- state -----------------------------------------------------------------------------

    def refresh(self) -> None:
        self.model_changed()

    def _data(self) -> dict:
        return self.win.model_config().get("data") or {}

    def model_changed(self) -> None:
        profile = self.win.profile
        mon = self.win.monitor
        config = self.win.model_config()
        state = config["state"]
        data = config.get("data") or {}
        problem = server_problem(
            self.app, profile, mon,
            purpose=_("Add an OpenDots server to choose the AI model its Dots think with."),
            waiting=_("Dotdeck keeps trying. The model shows up as soon as the server "
                      "answers."))
        if problem is None and state in ("unknown", "loading") and not data:
            problem = ("content-loading-symbolic", _("Checking the Server…"),
                       _("Asking {name} which model it uses.").format(name=profile.name))
        elif problem is None and state == "unsupported":
            problem = ("applications-science-symbolic",
                       _("This server does not let Dotdeck change its model"),
                       _("Its model is set in the server's .env file (OPENAI_MODEL and "
                         "OPENAI_API_KEY) and changes when OpenDots restarts."))
        elif problem is None and state == "error":
            problem = ("dialog-warning-symbolic", _("Could Not Read the Model"),
                       config.get("error") or "")
        editing = problem is None
        for group in (self.current, self.change, self.key, self.calls):
            group.set_visible(editing)
        if state == "unsupported" and problem is not None and mon is not None \
                and mon.status == "online":
            self.problem.show(problem, _("How to Change It"), lambda: self.win.open_url(GUIDE))
        elif state == "error" and problem is not None and mon is not None \
                and mon.status == "online":
            self.problem.show(problem, _("Try Again"),
                              lambda: self.win.request_model_config(force=True))
        elif problem is not None:
            self.problem.show_server(self.win, problem, profile, mon)
        else:
            self.problem.show(None)
        if not editing:
            self._signature = None
            return
        signature = (self.win.profile_id, json.dumps(data, sort_keys=True, default=str))
        if signature == self._signature:
            return
        self._signature = signature
        self._show_current(data)
        self._fill_change(data)
        self._fill_keys(data)
        self._show_calls(data)

    def _show_current(self, data: dict) -> None:
        model = str(data.get("model") or "")
        provider = models.provider_of(data)
        name = models.provider_name(provider, str(data.get("baseUrl") or "")) if provider \
            else _("Unknown")
        if model:
            self.model_row.set_title(escape(models.display_name(data)))
            self.model_row.set_subtitle(escape(f"{model} · {name}"))
        else:
            self.model_row.set_title(_("No Model Yet"))
            self.model_row.set_subtitle(escape(_("Choose one under Change.")))
        found = models.preset(data, model) if model else None
        price = models.price_text(data.get("price") if "price" in data
                                  else (found or {}).get("price"))
        self.price_row.set_subtitle(escape(price))
        self.price_row.set_visible(bool(price))

        configured = models.has_key(data, provider)
        self.key_status.set_title(escape(_("{provider} Key").format(provider=name)))
        self.key_status.set_subtitle(_("Configured") if configured else _("No key yet"))
        self.key_icon.set_from_icon_name("object-select-symbolic" if configured
                                         else "dialog-warning-symbolic")
        for css in ("success", "warning"):
            self.key_icon.remove_css_class(css)
        self.key_icon.add_css_class("success" if configured else "warning")

        # Only newer servers can test; a result stays until the model or provider changes.
        self.test_row.set_visible(models.modern(data))
        tested_for = (self.win.profile_id, model, provider)
        if tested_for != self._tested_for:
            self._tested_for = tested_for
            self._show_test(_("Sends a tiny message and times the answer."), "")
        self._sync_test()

    def _sync_test(self) -> None:
        data = self._data()
        ready = bool(data.get("model")) and data.get("configured") is not False
        self.test_button.set_sensitive(ready and not self._testing)

    def _show_test(self, text: str, css: str) -> None:
        self.test_row.set_subtitle(escape(text))
        icon = {"success": "object-select-symbolic",
                "warning": "dialog-warning-symbolic"}.get(css, "network-transmit-receive-symbolic")
        self.test_icon.set_from_icon_name(icon)
        for name in ("success", "warning"):
            self.test_icon.remove_css_class(name)
        if css:
            self.test_icon.add_css_class(css)

    # -- change: provider, model, address and key ------------------------------------------

    def _fill_change(self, data: dict) -> None:
        current = models.provider_of(data)
        modern = models.modern(data)
        listed = [p["id"] for p in models.providers(data) if p["id"] in models.CHOOSABLE]
        self._providers = (listed or list(models.CHOOSABLE)) if modern else []
        self._provider = current if current in self._providers or not modern \
            else self._providers[0]
        self._syncing = True
        try:
            self.provider_combo.set_model(string_model(
                [_(models.CHOICE_LABELS[p]) for p in self._providers]))
            if self._provider in self._providers:
                self.provider_combo.set_selected(self._providers.index(self._provider))
        finally:
            self._syncing = False
        self.provider_combo.set_visible(modern)
        self._fill_models(data)

    def _on_provider(self) -> None:
        if self._syncing:
            return
        index = self.provider_combo.get_selected()
        if 0 <= index < len(self._providers):
            self._provider = self._providers[index]
            self._fill_models(self._data())

    def select_provider(self, provider: str) -> None:
        """Pick ``provider`` under Change (where its key goes in with a model)."""
        if provider in self._providers:
            self.provider_combo.set_selected(self._providers.index(provider))

    def _fill_models(self, data: dict) -> None:
        modern = models.modern(data)
        target = self._provider
        current = models.provider_of(data)
        model = str(data.get("model") or "")
        same = not modern or target == current
        items = models.presets(data, target) if modern else models.presets(data)
        store = Gio.ListStore(item_type=Choice)
        self._choices = []
        if same and model and model not in [p["id"] for p in items]:
            store.append(Choice(model, models.display_name(data), _("In use")))
            self._choices.append(model)
        for item in items:
            # Older servers put the hint in the name: "DeepSeek V4 Flash (fast, low cost)".
            full = str(item.get("name") or item["id"])
            name = models.strip_hint(full)
            detail = models.preset_detail(item) or full[len(name):].strip(" ()")
            if same and item["id"] == model:
                detail = " · ".join(t for t in (_("In use"), detail) if t)
            store.append(Choice(item["id"], name, detail))
            self._choices.append(item["id"])
        store.append(Choice("", _("Custom…")))
        self._choices.append("")
        self._syncing = True
        try:
            self.combo.set_model(store)
            if same and model in self._choices:
                self.combo.set_selected(self._choices.index(model))
            else:
                self.combo.set_selected(0)
        finally:
            self._syncing = False

        self.custom.set_title({
            "openrouter": _("Model ID, for example vendor/model-name"),
            "anthropic": _("Model ID, for example claude-haiku-5-5"),
        }.get(target, _("Model ID")) if modern else _("Model ID, for example vendor/model-name"))
        compatible = modern and target == "openai-compatible"
        self.advanced.set_visible(compatible)
        if compatible:
            known = next((p["baseUrl"] for p in models.providers(data)
                          if p["id"] == target and p["baseUrl"]), "")
            if not known and current == target:
                known = str(data.get("baseUrl") or "")
            if not self.base_url.get_text().strip():
                self.base_url.set_text(known)
            if not known:
                self.advanced.set_expanded(True)
        switching = modern and target != current
        name = models.provider_name(target)
        self.new_key.set_title(
            _("New {provider} Key (optional)").format(provider=name)
            if models.has_key(data, target) else _("{provider} Key").format(provider=name))
        self.new_key.set_visible(switching)
        self._on_choice()

    def _selected_model(self) -> str:
        index = self.combo.get_selected()
        if not 0 <= index < len(self._choices):
            return ""
        return self._choices[index] or " ".join(self.custom.get_text().split())

    def _on_choice(self) -> None:
        index = self.combo.get_selected()
        custom = 0 <= index < len(self._choices) and self._choices[index] == ""
        self.custom.set_visible(custom)
        item = self.combo.get_selected_item()
        detail = item.props.detail if isinstance(item, Choice) and not custom else ""
        self.combo.set_subtitle(escape(detail))
        self._sync_apply()

    def _label(self, index: int) -> str:
        model = self.combo.get_model()
        item = model.get_item(index) if model is not None else None
        return item.props.name if isinstance(item, Choice) else ""

    def _pending(self) -> tuple[dict, str | None]:
        """What Switch would send, and why it cannot yet (None: it can)."""
        data = self._data()
        modern = models.modern(data)
        current = models.provider_of(data)
        target = self._provider if modern else current
        wanted = self._selected_model()
        changes: dict = {"model": wanted}
        problem = models.model_problem(target if modern else "", wanted)
        if modern and target != current:
            changes["provider"] = target
            if not models.has_key(data, target) and not self.new_key.get_text().strip():
                problem = problem or _("Type the {provider} key.").format(
                    provider=models.provider_name(target))
        if modern and target == "openai-compatible":
            url = self.base_url.get_text().strip()
            if url and url != data.get("baseUrl"):
                changes["baseUrl"] = url
            if url or target != current:
                problem = problem or models.base_url_problem(url)
        key = self.new_key.get_text().strip() if self.new_key.get_visible() else ""
        if key:
            changes["apiKey"] = key
        if problem is None and wanted == data.get("model") and set(changes) == {"model"}:
            problem = ""             # nothing to change
        return changes, problem

    def _sync_apply(self) -> None:
        if self._syncing:
            return
        _changes, problem = self._pending()
        index = self.combo.get_selected()
        custom = 0 <= index < len(self._choices) and self._choices[index] == ""
        typed = bool(self.custom.get_text().strip())
        if custom and typed and problem:
            self.custom.add_css_class("error")
            self.custom.set_tooltip_text(problem)
        else:
            self.custom.remove_css_class("error")
            self.custom.set_tooltip_text(None)
        valid = problem is None
        # Shown once something else is picked; a custom id needs to be typed first.
        self.apply.set_visible(valid or (custom and not self._busy) or
                               bool(problem and self.new_key.get_visible()))
        self.apply.set_sensitive(valid and not self._busy)

    # -- changes ---------------------------------------------------------------------------

    def _confirm(self) -> None:
        changes, problem = self._pending()
        profile = self.win.profile
        wanted = changes["model"]
        if problem is not None or profile is None or not self.apply.get_sensitive():
            return
        index = self.combo.get_selected()
        label = self._label(index) if self._choices[index] else wanted
        if "provider" in changes:
            body = _("Every Dot on {name} starts using it with its next message, through "
                     "{provider}. Chats keep their history.").format(
                name=profile.name, provider=models.provider_name(changes["provider"]))
        else:
            body = _("Every Dot on {name} starts using it with its next message. Chats keep "
                     "their history.").format(name=profile.name)
        confirm(self.win, _("Switch to {model}?").format(model=label), body,
                _("Switch Model"), lambda ok: ok and self._apply(wanted))

    def _apply(self, model: str) -> None:
        changes, problem = self._pending()
        if problem is not None or changes.get("model") != model:
            changes = {"model": model}
        self._busy = True
        self._sync_apply()
        name = models.display_name(self._data(), model) or model

        def done(ok: bool, error: str | None) -> None:
            self._busy = False
            if ok:
                self.win.toast(_("The Dots now use {model}").format(model=name))
                self.custom.set_text("")
                self.new_key.set_text("")
            else:
                self.win.toast(_("Could not change the model: {error}").format(error=error))
            self._signature = None
            self.model_changed()

        self.win.change_model_config(done, **changes)

    # -- keys ------------------------------------------------------------------------------

    def _fill_keys(self, data: dict) -> None:
        for row in self._key_rows:
            self.key.remove(row)
        self._key_rows = []
        if self.key_row.get_parent() is not None:
            self.key.remove(self.key_row)
        current = models.provider_of(data)
        for item in models.providers(data):
            provider = item["id"]
            configured = models.has_key(data, provider)
            row = Adw.ActionRow(title=escape(item["name"]),
                                subtitle=_("Configured") if configured else _("No key yet"))
            row.add_prefix(status_icon("object-select-symbolic" if configured
                                       else "dialog-warning-symbolic",
                                       "success" if configured else "warning"))
            if provider == current:
                row.add_suffix(suffix_label(_("In use")))
            else:
                button = Gtk.Button(label=_("Set New Key…"), valign=Gtk.Align.CENTER)
                button.add_css_class("flat")
                button.connect("clicked", lambda _b, p=provider: self.set_key_for(p))
                row.add_suffix(button)
            self.add(self.key, row, ("key", "api key", provider, item["name"]))
            self._key_rows.append(row)
            if provider == current:
                self.key_row.set_title(escape(_("Set a New {provider} Key").format(
                    provider=item["name"])))
                self.key.add(self.key_row)
        if self.key_row.get_parent() is None:      # the server reports no provider
            self.key_row.set_title(_("New API Key"))
            self.key.add(self.key_row)

    def set_key_for(self, provider: str) -> None:
        """A key for another provider goes in with a model of it, under Change."""
        self.select_provider(provider)
        self.new_key.set_visible(True)
        self.win.highlight(self.new_key)
        self.new_key.grab_focus()

    def _on_key(self, row: Adw.PasswordEntryRow) -> None:
        key = row.get_text().strip()
        if not key:
            return
        row.set_sensitive(False)

        def done(ok: bool, error: str | None) -> None:
            row.set_sensitive(True)
            if ok:
                row.set_text("")
                self.win.toast(_("API key saved on the server"))
                self._signature = None
                self.model_changed()
            else:
                self.win.toast(_("Could not save the key: {error}").format(error=error))

        self.win.change_model_config(done, apiKey=key)

    # -- test ------------------------------------------------------------------------------

    def run_test(self) -> None:
        if self._testing:
            return
        self._testing = True
        self._sync_test()
        self._show_test(_("Testing…"), "")
        asked = self._tested_for

        def done(data, error) -> None:
            self._testing = False
            self._sync_test()
            if asked != self._tested_for:
                return                   # the model changed meanwhile
            if error == "unsupported":
                self._show_test(_("This server cannot test the model yet."), "warning")
            elif error is not None:
                self._show_test(_("Could not test: {error}").format(error=error), "warning")
            elif data.get("ok"):
                self._show_test(_("Answered in {time}").format(
                    time=models.seconds(data.get("ms"))), "success")
            else:
                self._show_test(_("Did not answer: {error}").format(
                    error=data.get("error") or _("unknown error")), "warning")

        self.win.try_model(done)

    # -- voice calls -----------------------------------------------------------------------

    def _show_calls(self, data: dict) -> None:
        call = models.call_models(data)
        voice = call["voice"]
        if not voice:
            speech = _("Not set up on the server")
        elif call["gateway"]:
            speech = _("{model} · Dotdeck voice gateway").format(model=voice)
        else:
            speech = _("{model} · OpenAI Realtime").format(model=voice)
        self.speech_row.set_subtitle(escape(speech))
        if call["transcribe"]:
            transcribe = call["transcribe"]
        elif call["reported"]:
            transcribe = _("Off, or the voice gateway did not answer")
        else:
            transcribe = _("This server does not report it")
        self.transcribe_row.set_subtitle(escape(transcribe))
        thinking = call["thinking"]
        self.thinking_row.set_subtitle(escape(
            _("{model} · the chat model").format(model=models.display_name(data, thinking))
            if thinking else "—"))
