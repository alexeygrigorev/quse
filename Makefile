publish-build:
	uv run hatch build

publish-clean:
	rm -r dist/

# Release: tag the current version and push to trigger CI publish.
release:
	@VERSION=$$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -1); \
	if test -z "$$VERSION"; then echo "Could not determine version from pyproject.toml" >&2; exit 1; fi; \
	echo "Releasing v$$VERSION"; \
	git tag "v$$VERSION"; \
	git push origin "v$$VERSION"
