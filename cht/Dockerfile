FROM python:3.11.11

ENV TZ=Asia/Taipei

RUN apt update
RUN apt install vim -y
RUN pip install --upgrade pip
# cryptography / asn1crypto:mock 現在會真的簽章(見 cms_sign.py),
# 不再回寫死的 PKCS#7。版本對齊 services/csp 用的那一組。
RUN pip install flask cryptography asn1crypto
