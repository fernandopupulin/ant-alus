FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS observations

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 SKIP_INTEGRATION=0 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --no-deps -r /tmp/requirements.lock
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir /src /app && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /src \
    && rm /tmp/sqlglot.tar.gz /tmp/requirements.lock
COPY coverage_map.py comparisons.py /app/
WORKDIR /src
RUN PYTHONPATH=/src python /app/coverage_map.py /app/coverage.json.gz

FROM observations AS selector
RUN python -m compileall -q --invalidation-mode checked-hash /src/sqlglot /src/tests
COPY selector.py /app/selector.py
CMD ["python", "/app/selector.py"]
