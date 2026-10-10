"""YOUR DOTS › AI Model: which model the server's Dots think with, and its key.

Only servers with ``/api/model-config`` (set up with the Dotdeck guide) let Dotdeck
change it; on others the page says where the model is set. Keys are write-only: the
server says whether one is configured and Dotdeck never shows or keeps it.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ... import config
from ...i18n import _
from .common import SettingsPage, confirm, escape, string_model
from .dots import ProblemGroup, server_problem

# Where OpenDots explains its .env (OPENAI_MODEL, OPENAI_API_KEY).
GUIDE = f"{config.OPENDOTS_URL}#get-started"
PROVIDERS = {"openrouter": "OpenRouter", "gemini": "Google Gemini", "claude": "Anthropic",
             "openai": "OpenAI", "openai-compatible": "OpenAI-compatible"}


def provider_name(provider: str, base_url: str = "") -> str:
    if provider in PROVIDERS:
        return PROVIDERS[provider]
    if "openrouter.ai" in base_url:
        return PROVIDERS["openrouter"]
    return provider or _("Unknown")


class ModelPage(SettingsPage):
    name = "model"
    title = _("AI Model")
    icon = "applications-science-symbolic"

    def build(self) -> None:
        self._signature = None
        self._choices: list[str] = []
        self._syncing = False
        self._busy = False

        top = self.group()
        self.selector = self.add(top, self.win.server_selector(), ("server",))
        top.set_visible(self.selector.get_visible())
        self.selector.connect("notify::visible",
                              lambda row, _p: top.set_visible(row.get_visible()))
        self.problem = ProblemGroup()
        self.widget.add(self.problem)

        self.current = self.group(_("Current Model"))
        self.model_row = Adw.ActionRow(title=_("Model"), subtitle_selectable=True)
        self.add(self.current, self.model_row, ("llm", "model", "brain", "ai"))
        self.provider_row = Adw.ActionRow(title=_("Provider"))
        self.add(self.current, self.provider_row, ("provider", "openrouter", "openai"))
        self.endpoint_row = Adw.ActionRow(title=_("Endpoint"), subtitle_selectable=True)
        self.add(self.current, self.endpoint_row, ("base url", "endpoint", "api url"))

        self.change = self.group(
            _("Change Model"),
            _("Every Dot on the server uses it from its next message. The server keeps the "
              "choice in its .env file."))
        self.combo = Adw.ComboRow(title=_("Model"), enable_search=True, use_subtitle=True)
        if hasattr(Adw, "ComboRowSearchMatchMode"):
            self.combo.set_search_match_mode(Adw.ComboRowSearchMatchMode.SUBSTRING)
        self.combo.connect("notify::selected", lambda *_: self._on_choice())
        self.add(self.change, self.combo, ("preset", "deepseek", "claude", "gemini", "gpt"))
        self.custom = Adw.EntryRow(title=_("Model ID, for example vendor/model-name"),
                                   visible=False)
        self.custom.connect("changed", lambda _r: self._sync_apply())
        self.custom.connect("entry-activated", lambda _r: self._confirm())
        self.add(self.change, self.custom, ("custom", "model id"))
        self.apply = Adw.ButtonRow(title=_("Switch Model…"), visible=False)
        self.apply.add_css_class("suggested-action")
        self.apply.connect("activated", lambda _r: self._confirm())
        self.add(self.change, self.apply, ("apply", "switch", "change"))

        self.key = self.group(_("API Key"),
                              _("Type a new key to replace the one on the server. Dotdeck "
                                "never shows it and does not keep it."))
        self.key_status = Adw.ActionRow(title=_("Current Key"))
        self.key_icon = Gtk.Image(valign=Gtk.Align.CENTER)
        self.key_status.add_prefix(self.key_icon)
        self.add(self.key, self.key_status, ("key", "configured", "api key"))
        self.key_row = Adw.PasswordEntryRow(title=_("New API Key"), show_apply_button=True)
        self.key_row.connect("apply", self._on_key)
        self.add(self.key, self.key_row, ("key", "token", "secret", "api key"))
        self.refresh()

    # -- state -----------------------------------------------------------------------------

    def refresh(self) -> None:
        self.model_changed()

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
        for group in (self.current, self.change, self.key):
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
        signature = (self.win.profile_id, data.get("model"), data.get("baseUrl"),
                     data.get("provider"), bool(data.get("hasApiKey")),
                     tuple(p.get("id") for p in self._presets(data)))
        if signature == self._signature:
            return
        self._signature = signature
        model = str(data.get("model") or "")
        self.model_row.set_subtitle(escape(model or _("Not set")))
        self.provider_row.set_subtitle(escape(provider_name(str(data.get("provider") or ""),
                                                            str(data.get("baseUrl") or ""))))
        self.endpoint_row.set_subtitle(escape(str(data.get("baseUrl") or "—")))
        configured = bool(data.get("hasApiKey"))
        self.key_status.set_subtitle(_("Configured") if configured else _("Not set"))
        self.key_icon.set_from_icon_name("object-select-symbolic" if configured
                                         else "dialog-warning-symbolic")
        for css in ("success", "warning"):
            self.key_icon.remove_css_class(css)
        self.key_icon.add_css_class("success" if configured else "warning")

        presets = self._presets(data)
        self._choices = [p["id"] for p in presets]
        labels = [p.get("name") or p["id"] for p in presets]
        if model and model not in self._choices:
            self._choices.insert(0, model)
            labels.insert(0, model)
        self._choices.append("")
        labels.append(_("Custom…"))
        self._syncing = True
        try:
            self.combo.set_model(string_model(labels))
            self.combo.set_selected(self._choices.index(model) if model in self._choices
                                    else len(self._choices) - 1)
        finally:
            self._syncing = False
        self._on_choice()

    @staticmethod
    def _presets(data: dict) -> list[dict]:
        presets = data.get("presets") if isinstance(data.get("presets"), list) else []
        return [p for p in presets if isinstance(p, dict) and isinstance(p.get("id"), str)
                and p["id"]]

    def _selected_model(self) -> str:
        index = self.combo.get_selected()
        if not 0 <= index < len(self._choices):
            return ""
        return self._choices[index] or " ".join(self.custom.get_text().split())

    def _on_choice(self) -> None:
        index = self.combo.get_selected()
        custom = 0 <= index < len(self._choices) and self._choices[index] == ""
        self.custom.set_visible(custom)
        self._sync_apply()

    def _label(self, index: int) -> str:
        model = self.combo.get_model()
        item = model.get_item(index) if model is not None else None
        return item.get_string() if item is not None else ""

    def _sync_apply(self) -> None:
        data = self.win.model_config().get("data") or {}
        wanted = self._selected_model()
        index = self.combo.get_selected()
        custom = 0 <= index < len(self._choices) and self._choices[index] == ""
        valid = bool(wanted) and wanted != data.get("model") and " " not in wanted
        # Shown once another model is picked; a custom id needs to be typed first.
        self.apply.set_visible(valid or (custom and not self._busy))
        self.apply.set_sensitive(valid and not self._busy)

    # -- changes ---------------------------------------------------------------------------

    def _confirm(self) -> None:
        wanted = self._selected_model()
        profile = self.win.profile
        if not wanted or profile is None or not self.apply.get_sensitive():
            return
        index = self.combo.get_selected()
        label = self._label(index) if self._choices[index] else wanted
        confirm(self.win, _("Switch to {model}?").format(model=label),
                _("Every Dot on {name} starts using it with its next message. Chats keep "
                  "their history.").format(name=profile.name),
                _("Switch Model"), lambda ok: ok and self._apply(wanted))

    def _apply(self, model: str) -> None:
        self._busy = True
        self._sync_apply()

        def done(ok: bool, error: str | None) -> None:
            self._busy = False
            if ok:
                self.win.toast(_("The Dots now use {model}").format(model=model))
                self.custom.set_text("")
            else:
                self.win.toast(_("Could not change the model: {error}").format(error=error))
            self._signature = None
            self.model_changed()

        self.win.change_model_config(done, model=model)

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
