# Release Asset Check


This integration uses a HACS ZIP release asset. Before publishing a release, build and validate the asset from the exact tag:

```bash
python3 scripts/build_hacs_release_asset.py vX.Y.Z dist/diesel-level-thermostat.zip
```

Attach that generated ZIP when creating the GitHub release. Do not publish a
release before its validated ZIP is attached. The verifier checks all runtime
files, parses Python, decodes all eight transparent brand images, and compares
every byte with the exact Git ref. CI also builds an unpublished preview from
`HEAD`. The release workflow checks that the published asset is downloadable
and still matches the release tag. It never creates or publishes a release.
