# Dotdeck — common developer and packaging tasks.
#
#   make run            run from the source tree
#   make test           unit, WebKit and voice tests (under xvfb + dbus when available)
#   make smoke          drive the UI once and fail on errors (OPENDOTS_CLIENT=<OpenDots>/dist/client
#                       uses the real web client instead of the stand-in page)
#   make contract       check src/web/contract.json against OPENDOTS=<an OpenDots checkout>
#   make page-strings   texts of the OpenDots page (OPENDOTS=<checkout>) missing from
#                       src/web/translations
#   make screenshots    refresh data/screenshots (OPENDOTS_CLIENT=<OpenDots>/dist/client)
#   make store-assets   the Snap Store icon, banner and slides (data/store)
#   make site           the website in build/website (build-aux/website.sh)
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

.PHONY: all run test smoke contract page-strings screenshots store-assets site icon validate privacy locale install uninstall \
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
	$(HEADLESS) env GDK_BACKEND=x11 $(PYTHON) -W ignore tools/screenshots.py --smoke --out build/smoke \
		$(if $(OPENDOTS_CLIENT),--demo "$(OPENDOTS_CLIENT)")

# The classes, labels, words and routes Dotdeck needs, looked up in an OpenDots checkout
# (build it first for the checks on the bundle: npm ci && npm run build).
page-strings:
	@test -d "$(OPENDOTS)/src/client" || { echo "Use: make page-strings OPENDOTS=<OpenDots>"; exit 1; }
	@for catalog in src/web/translations/*.json; do node tools/page_strings.cjs "$(OPENDOTS)" "$$catalog" || exit 1; done

contract:
	@test -d "$(OPENDOTS)/src/client" || { echo "Use: make contract OPENDOTS=<OpenDots>"; exit 1; }
	OPENDOTS_SRC="$(OPENDOTS)" $(PYTHON) -m unittest -v tests.test_contract

# English for AppStream (data/screenshots), Brazilian Portuguese for the site and the
# store (data/screenshots/pt_BR), light and dark, all with fictional Dots on the real
# OpenDots page: OPENDOTS_CLIENT is the dist/client folder of an OpenDots build.
SHOTS := welcome connected workspace pets settings-dots settings-voice settings-model
SHOOT  = $(HEADLESS) env GDK_BACKEND=x11 $(PYTHON) -W ignore tools/screenshots.py \
	--demo "$(OPENDOTS_CLIENT)"

screenshots: locale
	@test -f "$(OPENDOTS_CLIENT)/index.html" || \
		{ echo "Use: make screenshots OPENDOTS_CLIENT=<OpenDots>/dist/client"; exit 1; }
	rm -rf build/shots
	LANGUAGE=en LANG=C.UTF-8 $(SHOOT) --out build/shots/en
	LANGUAGE=en LANG=C.UTF-8 $(SHOOT) --dark --out build/shots/en
	LANGUAGE=pt_BR LANG=pt_BR.UTF-8 $(SHOOT) --out build/shots/pt_BR
	LANGUAGE=pt_BR LANG=pt_BR.UTF-8 $(SHOOT) --dark --out build/shots/pt_BR
	mkdir -p data/screenshots/pt_BR
	for shot in $(SHOTS); do \
		cp build/shots/en/$$shot.png data/screenshots/ && \
		cp build/shots/pt_BR/$$shot.png build/shots/pt_BR/$$shot-dark.png \
			data/screenshots/pt_BR/ || exit 1; \
	done
	cp build/shots/en/workspace-dark.png data/screenshots/

site:
	build-aux/website.sh build/website

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
