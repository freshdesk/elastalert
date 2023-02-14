FROM python:2.7-alpine as build

ENV ELASTALERT_HOME /opt/elastalert
ADD . /opt/elastalert/

WORKDIR /opt

RUN apk add --update --no-cache gcc openssl-dev libffi-dev openssl ca-certificates musl-dev python-dev
RUN  pip install "setuptools==36.2.7" "elasticsearch==6.3.1"

WORKDIR "${ELASTALERT_HOME}"

RUN pip install -r requirements-new.txt
RUN  python setup.py install

FROM gcr.io/distroless/python2.7:debug as runtime

COPY --from=build /opt/elastalert /opt/elastalert
COPY --from=build /usr/local/lib/python2.7 /usr/local/lib/python2.7
COPY --from=build /usr/local/bin/elastalert* /usr/local/bin/
COPY --from=build /usr/local/lib/libpython2.7.so.1.0 /usr/local/lib/
COPY --from=build /usr/lib/libpython2.7.so.1.0 /usr/lib/
COPY --from=build /lib/libc.musl-x86_64.so.1 /lib/

#COPY  --from=build /data/elastalert /data/elastalert

ENV PYTHONPATH=/usr/local/lib/python2.7/site-packages
ENV PATH=/usr/local/lib:/usr/lib:$PATH

WORKDIR /opt/elastalert

CMD ["/usr/local/bin/elastalert-create-index","--config","/data/elastalert/config.yaml", "--verbose"]
CMD ["/usr/local/bin/elastalert","--config","/data/elastalert/config.yaml", "--verbose"]
# FROM python:3-slim-buster as builder

# LABEL description="ElastAlert 2 Official Image"
# LABEL maintainer="Jason Ertel"

# COPY . /tmp/elastalert

# RUN mkdir -p /opt/elastalert && \
#     cd /tmp/elastalert && \
#     pip install setuptools wheel && \
#     python setup.py sdist bdist_wheel

# FROM python:3-slim-buster

# ARG GID=1000
# ARG UID=1000
# ARG USERNAME=elastalert

# COPY --from=builder /tmp/elastalert/dist/*.tar.gz /tmp/

# RUN apt update && apt -y upgrade && \
#     apt -y install jq curl gcc libffi-dev && \
#     rm -rf /var/lib/apt/lists/* && \
#     pip install /tmp/*.tar.gz && \
#     rm -rf /tmp/* && \
#     apt -y remove gcc libffi-dev && \
#     apt -y autoremove && \
#     mkdir -p /opt/elastalert && \
#     echo "#!/bin/sh" >> /opt/elastalert/run.sh && \
#     echo "set -e" >> /opt/elastalert/run.sh && \
#     echo "elastalert-create-index --config /opt/elastalert/config.yaml" \
#         >> /opt/elastalert/run.sh && \
#     echo "elastalert --config /opt/elastalert/config.yaml \"\$@\"" \
#         >> /opt/elastalert/run.sh && \
#     chmod +x /opt/elastalert/run.sh && \
#     groupadd -g ${GID} ${USERNAME} && \
#     useradd -u ${UID} -g ${GID} -M -b /opt -s /sbin/nologin \
#         -c "ElastAlert 2 User" ${USERNAME}

# USER ${USERNAME}
# ENV TZ "UTC"

# WORKDIR /opt/elastalert
# ENTRYPOINT ["/opt/elastalert/run.sh"]
