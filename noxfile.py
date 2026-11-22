import nox
import shutil

# Supported Python version matrix
PYTHON_VERSIONS = ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]

# Tell Nox to ONLY run tests by default when typing "nox"
nox.options.sessions = ["tests"]


@nox.session(python=PYTHON_VERSIONS)
def tests(session):
    """Run the test suite via unittest."""
    session.install("-e", ".")
    args = session.posargs or ["discover", "-s", "tests"]
    session.run("python", "-m", "unittest", *args)


@nox.session
def lint(session):
    """Check code formatting and style guidelines using Ruff."""
    session.install("ruff")
    session.run("ruff", "check", "pyunormalize", "tests", "tools")


@nox.session
def build(session):
    """Build source and wheel distributions, then validate with twine."""
    session.install("build", "twine")

    # Clean up previous distribution artifacts
    shutil.rmtree("dist", ignore_errors=True)

    # Build sdist and wheel packages
    session.run("python", "-m", "build")

    # Validate package metadata and README formatting for PyPI
    session.run("twine", "check", "dist/*")
