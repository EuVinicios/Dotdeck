# Dotdeck — common developer and packaging tasks.
#
#   make run            run from the source tree
#   make test           unit, WebKit and voice tests (under xvfb + dbus when available)
#   make smoke          drive the UI once and fail on errors
#   make screenshots    refresh data/screenshots (DEMO_URL=… DEMO_TOKEN=… from tools/demo_server.py)
#   make icon           re-render the plush app icon (tools/render_icon.py)
#   make validate       desktop file, AppStream and release checks
#   make privacy        no keys, tokens, home folders or private addresses in the tree
#   make install        install system-wide (PREFIX=/usr/local, DESTDIR supported)
#   make deb            build dist/dotdeck_<version>_all.deb
#   make flatpak        build and install the Flatpak locally (needs org.flatpak.Builder)
#   make flatpak-lint   run the Flathub linter on the manifest and the build
#   make snap           build the snap (needs snapcraft and LXD)
#   make pot            refresh po/dotdeck.pot and merge the translations

APP_ID      := io.github.euvinicios.Dotdeck
PREFIX      ?= /usr/local
PYTHON      ?= /usr/bin/python3
DESTDIR     ?=
BINDIR      := $(PREFIX)/bin
DATADIR     := $(PREFIX)/share
PKGDATADIR  := $(DATADIR)/dotdeck
VERSION     := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' src/config.py)
LINGUAS     := $(shell cat po/LINGUAS 2>/dev/null)
ICON_SIZES  := 64 128 256 512
FLATPAK_BUILDER ?= $(shell command -v flatpak-builder 2>/dev/null || echo flatpak run org.flatpak.Builder)

HEADLESS := $(shell command -v dbus-run-session >/dev/null 2>&1 && echo "dbus-run-session --") \
            $(shell command -v xvfb-run >/dev/null 2>&1 && echo "xvfb-run -a -s '-screen 0 1600x1000x24'")

.PHONY: all run test smoke screenshots store-assets icon validate privacy locale install uninstall \
	deb flatpak flatpak-lint snap pot clean version

all: locale

run: locale
	$(PYTHON) -m src

version:
	@echo $(VERSION)

test:
	$(HEADLESS) env GDK_BACKEND=x11 DOTDECK_SECRETS=memory DOTDECK_TEST_AUDIO=1 \
		$(PYTHON) -W ignore::DeprecationWarning -m unittest discover -s tests -t . -v

smoke:
	$(HEADLESS) env GDK_BACKEND=x11 $(PYTHON) -W ignore tools/screenshots.py --smoke --out build/smoke

screenshots:
	$(HEADLESS) env GDK_BACKEND=x11 $(PYTHON) -W ignore tools/screenshots.py $(if $(DEMO_URL),--url $(DEMO_URL) --token $(DEMO_TOKEN))
	$(HEADLESS) env GDK_BACKEND=x11 $(PYTHON) -W ignore tools/screenshots.py --dark --out build/dark $(if $(DEMO_URL),--url $(DEMO_URL) --token $(DEMO_TOKEN))
	cp build/dark/workspace-dark.png data/screenshots/

store-assets:
	$(PYTHON) tools/store_assets.py

icon:
	$(PYTHON) tools/render_icon.py data/icons/hicolor

validate:
	desktop-file-validate data/$(APP_ID).desktop
	appstreamcli validate --no-net --explain data/$(APP_ID).metainfo.xml
	@grep -q '<release version="$(VERSION)"' data/$(APP_ID).metainfo.xml || \
		{ echo "metainfo sem <release> para $(VERSION)"; exit 1; }
	@for lang in $(LINGUAS); do msgfmt --check -o /dev/null po/$$lang.po || exit 1; done

privacy:
	$(PYTHON) tools/privacy_scan.py

locale:
	@for lang in $(LINGUAS); do \
		mo=src/locale/$$lang/LC_MESSAGES/dotdeck.mo; \
		if [ -f $$mo ] && [ $$mo -nt po/$$lang.po ]; then continue; fi; \
		mkdir -p $$(dirname $$mo); \
		msgfmt -o $$mo po/$$lang.po || exit 1; \
	done

pot:
	xgettext --from-code=UTF-8 --language=Python --keyword=_ --keyword=N_ --keyword=ngettext:1,2 \
		--package-name=Dotdeck --package-version=$(VERSION) --add-comments=TRANSLATORS \
		-o po/dotdeck.pot $$(cat po/POTFILES)
	@for lang in $(LINGUAS); do msgmerge --update --backup=none --no-fuzzy-matching po/$$lang.po po/dotdeck.pot; done

install: locale
	install -d "$(DESTDIR)$(PKGDATADIR)/dotdeck"
	cp -R src/. "$(DESTDIR)$(PKGDATADIR)/dotdeck/"
	find "$(DESTDIR)$(PKGDATADIR)" -name __pycache__ -prune -exec rm -rf {} +
	install -d "$(DESTDIR)$(BINDIR)"
	sed -e 's|@PYTHON@|$(PYTHON)|' -e 's|@PKGDATADIR@|$(PKGDATADIR)|' build-aux/dotdeck.in \
		> "$(DESTDIR)$(BINDIR)/dotdeck"
	chmod 755 "$(DESTDIR)$(BINDIR)/dotdeck"
	install -Dm644 data/$(APP_ID).desktop "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop"
	install -Dm644 data/$(APP_ID).metainfo.xml "$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml"
	for size in $(ICON_SIZES); do \
		install -Dm644 data/icons/hicolor/$${size}x$${size}/apps/$(APP_ID).png \
			"$(DESTDIR)$(DATADIR)/icons/hicolor/$${size}x$${size}/apps/$(APP_ID).png"; \
	done
	install -Dm644 data/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg \
		"$(DESTDIR)$(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg"

uninstall:
	rm -rf "$(DESTDIR)$(PKGDATADIR)"
	rm -f "$(DESTDIR)$(BINDIR)/dotdeck" \
		"$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop" \
		"$(DESTDIR)$(DATADIR)/metainfo/$(APP_ID).metainfo.xml" \
		"$(DESTDIR)$(DATADIR)/icons/hicolor/symbolic/apps/$(APP_ID)-symbolic.svg"
	for size in $(ICON_SIZES); do \
		rm -f "$(DESTDIR)$(DATADIR)/icons/hicolor/$${size}x$${size}/apps/$(APP_ID).png"; \
	done

deb:
	build-aux/deb/build-deb.sh

flatpak:
	$(FLATPAK_BUILDER) --disable-rofiles-fuse --user --install --install-deps-from=flathub --force-clean \
		build/flatpak build-aux/flatpak/$(APP_ID).json

flatpak-lint:
	flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest \
		build-aux/flatpak/$(APP_ID).json
	flatpak run --command=flatpak-builder-lint org.flatpak.Builder appstream \
		data/$(APP_ID).metainfo.xml

snap:
	snapcraft pack --output dist/

clean:
	rm -rf build dist src/locale .flatpak-builder repo
	find . -name __pycache__ -prune -exec rm -rf {} +
