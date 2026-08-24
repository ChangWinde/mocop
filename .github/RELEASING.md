# Releasing Mocop

Release tags are immutable public installation identities. Create one only from
the exact default-branch commit whose CI run is complete and successful.

Repository rules enforce the release boundary: `master` accepts changes only
through a pull request with every CI job successful, and complementary `v*` tag
rulesets allow only the repository owner to create version tags while denying all
tag updates and deletions. Automatic head-branch deletion keeps merged refs from
accumulating.

1. In a pull request, update `pyproject.toml` and `src/mocop/__init__.py` to the same
   SemVer value, move finished entries below a dated Changelog heading, and pin
   both onboarding READMEs to that tag.
2. Run every command in [Contributing](CONTRIBUTING.md), review the complete diff,
   and merge only after the required default-branch checks pass.
3. Verify the merge commit's push workflow on GitHub, then create and push one
   annotated tag:

   ```bash
   git switch master
   git pull --ff-only
   git tag -a vX.Y.Z -m "Mocop X.Y.Z"
   git push origin vX.Y.Z
   ```

4. The tag workflow validates the tag/version/Changelog contract, builds the
   wheel, and creates the GitHub Release from the versioned notes. Verify the tag
   CI and release workflow belong to the tagged SHA, verify the attached SHA-256
   manifest, download the wheel, and run `mocop --help` from an isolated environment.
5. Never move, replace, or delete a published version tag. Correct a release with
   a new patch version and explain the supersession in the Changelog.

PyPI publication is not currently automated or promised. The GitHub tag, source
archive, and attached wheel are the supported release artifacts.
