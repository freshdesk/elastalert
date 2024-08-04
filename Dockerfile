FROM public.ecr.aws/i1i0w6p5/python:3.9.2 as build

ENV ELASTALERT_HOME /opt/elastalert
ADD . /opt/elastalert/

WORKDIR /opt

RUN pip install "setuptools==65.5.0" "elasticsearch==7.10.1"

WORKDIR "${ELASTALERT_HOME}"

RUN pip install -r requirements.txt
RUN python setup.py install

RUN pip show elastalert2

RUN python --version

WORKDIR /opt/elastalert

COPY commands.sh /opt/elastalert/commands.sh
RUN ["chmod", "+x", "/opt/elastalert/commands.sh"]
ENTRYPOINT ["sh","/opt/elastalert/commands.sh"]
