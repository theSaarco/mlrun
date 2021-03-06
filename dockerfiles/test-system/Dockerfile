# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
ARG MLRUN_PYTHON_VERSION=3.7

FROM python:${MLRUN_PYTHON_VERSION}-slim

ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install --no-install-recommends -y \
  gcc \
  git-core \
 && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip~=20.2.0

COPY ./dockerfiles/test-system/requirements.txt /tmp
RUN python -m pip install -r /tmp/requirements.txt && rm -f /tmp/requirements.txt
COPY . /tmp/mlrun
RUN cd /tmp/mlrun && python -m pip install ".[complete]" && mv tests /tests && cd /tmp && rm -rf mlrun

CMD ["pytest",  "--capture=no", "-rf",  "-v",  "--disable-warnings", "/tests/system"]
