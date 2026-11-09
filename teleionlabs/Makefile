# teleionlabs.com. Light only (MUST-FOLLOW rule 13). Stdlib Python and Node 22; the browser check needs a running copy.
PYTHON ?= python3
BASE ?= http://127.0.0.1:8799
# A public copy has no private word list or media: these let `make` run its checks there, and say so. A deploy runs build.py with neither.
export TELEION_SAMPLE_WORD_LIST = 1
export MEDIA_BASE ?= https://teleionlabs.com

.PHONY: film-fixture build test range-test words links check light-check

build:
	$(PYTHON) build.py

film-fixture:
	$(PYTHON) tests/make_film_fixture.py

test: film-fixture build
	$(PYTHON) tests/test_t7_repo_links.py
	$(PYTHON) tests/test_notice_exempt.py
	$(PYTHON) tests/test_fabric_worker_py.py
	node --test tests/word_list_refusal.test.mjs tests/worker.test.mjs tests/worker_example.test.mjs tests/test_fabric_worker_mjs.mjs tests/network.test.mjs tests/network_client.test.mjs

# Real workerd streaming/Content-Length checks using the existing workspace install.
range-test: film-fixture
	@if [ -n "$$MINIFLARE" ] || [ -d ../../../portal-worker/node_modules/miniflare ]; then node --test tests/media_range.test.mjs tests/network_runtime.test.mjs; else echo "range test skipped: set MINIFLARE to a miniflare install"; fi

words: build
	$(PYTHON) tests/check_words.py

links: build
	$(PYTHON) tests/check_links.py

check: test range-test words links

# Light, layout and axe on every page under both colour schemes, against BASE (wrangler dev or a deploy).
light-check: build
	node tests/check_site.mjs --base $(BASE)
