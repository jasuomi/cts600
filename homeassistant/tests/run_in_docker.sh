#!/bin/sh
# Run the integration tests in a clean Linux container (Home Assistant
# doesn't install on Windows):
#   docker run --rm -v "$PWD/homeassistant:/src:ro" python:3.14 sh /src/tests/run_in_docker.sh
set -e
cp -r /src /work
cd /work
pip install -q --root-user-action=ignore pytest-homeassistant-custom-component
python -c "import homeassistant.const as c; print('Home Assistant', c.__version__)"
python -m pytest tests -q -p no:cacheprovider
