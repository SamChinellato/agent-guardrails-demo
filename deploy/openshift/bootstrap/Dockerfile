# The gateway-bootstrap image (OS-10, D-055): scripts/gateway-apply.sh with what it needs
# (bash, curl, jq). Built in aigw-build by scripts/openshift/build-images.sh; every input
# is pinned in versions.env.
ARG PYTHON_BASE_REF
FROM ${PYTHON_BASE_REF}
ARG CURL_VERSION
ARG JQ_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends "curl=${CURL_VERSION}" "jq=${JQ_VERSION}" \
 && rm -rf /var/lib/apt/lists/*
COPY gateway-apply.sh /usr/local/bin/gateway-apply.sh
# Any UID in group 0 (restricted-v2); the script writes only to mktemp's /tmp
USER 1001
ENTRYPOINT ["/usr/local/bin/gateway-apply.sh"]
