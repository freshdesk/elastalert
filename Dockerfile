FROM python:3.9-alpine@sha256:9e5d43f27e1e9f3bf9dd06c37dce082801b87564bb0f1d0746174709026b80a1 as build

ENV ELASTALERT_HOME /opt/elastalert
ADD . /opt/elastalert/

WORKDIR /opt

RUN apk add --update --no-cache jq curl gcc openssl-dev libffi-dev ca-certificates musl-dev
#RUN apk add --update --no-cache gcc libffi-dev
#RUN apk info -a | grep curl
#RUN apk info -a | grep openssl-dev
#RUN apk search -v curl
#RUN apk search -v openssl-dev

#RUN apk add --no-cache \
#    jq=1.7.1-r0 \
#    curl=8.7.1-r0 \
#    gcc=13.2.1_git20240309-r0 \
#    openssl-dev=3.3.1-r0 \
#    libffi-dev=3.4.6-r0 \
#    ca-certificates=20240226-r0 \
#    musl-dev=1.2.5-r0

RUN pip install "setuptools==65.5.0" "elasticsearch==6.3.1"

WORKDIR "${ELASTALERT_HOME}"

RUN pip install -r requirements.txt
RUN python setup.py install

RUN pip show elastalert2


FROM gcr.io/distroless/python3:debug@sha256:e5eb1348c23118d52d03defafa1eddf3a0aea116bd08711cc31ebf657d7fd040 as runtime

COPY --from=build /opt/elastalert /opt/elastalert
COPY --from=build /usr/local/bin/elastalert* /usr/local/bin/

COPY --from=build /opt/elastalert /opt/elastalert 
COPY --from=build /usr/local/lib/python3.9 /usr/local/lib/python3.9
COPY --from=build /usr/local/bin/elastalert* /usr/local/bin/
COPY --from=build /usr/local/lib/libpython3.9.so.1.0 /usr/local/lib/
COPY --from=build /lib/libc.musl-x86_64.so.1 /lib/

#COPY  --from=build /data/elastalert /data/elastalert

ENV PYTHONPATH=/usr/local/lib/python3.9/site-packages
ENV PATH=/usr/local/lib:/usr/lib:$PATH

RUN python --version

WORKDIR /opt/elastalert

COPY commands.sh /opt/elastalert/commands.sh
RUN ["chmod", "+x", "/opt/elastalert/commands.sh"]
ENTRYPOINT ["sh","/opt/elastalert/commands.sh"]
