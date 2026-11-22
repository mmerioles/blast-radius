# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

# Same package set the organisers labeled with.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, from its own repository.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /opt/sqlglot \
 && cd /opt/sqlglot && python -m compileall -q -j 4 sqlglot tests > /dev/null

# Per-test coverage / class-usage / file-read maps of the pristine checkout.
COPY sample/tests.json /app/tests.json
COPY br /app/br
COPY tools/build_maps.py /app/tools/build_maps.py
RUN mkdir -p /opt/brmaps \
 && cd /opt/sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=/opt/sqlglot \
    PYTHONHASHSEED=11 python /app/tools/build_maps.py /opt/sqlglot /app/tests.json /opt/brmaps/maps.pickle 4 \
    2>&1 | grep -E "^maps|value profile|forward pass|Error" ; test -s /opt/brmaps/maps.pickle \
 && chmod -R a+rX /opt/sqlglot /opt/brmaps /app

COPY selector.py /app/selector.py
RUN python -m compileall -q /app && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
