# Copyright 2016: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import hashlib
import logging

import flask

from notify import config
from notify import driver


LOG = logging.getLogger("api")
LOG.setLevel(config.get_config().get("logging", {}).get("level", "INFO"))


bp = flask.Blueprint("notify", __name__)


CACHE = {}


def make_hash(dct):
    """Generate MD5 hash of given dict.

    :param dct: dict to hash. There may be collisions
                if it isn't flat (includes sub-dicts)
    :returns: str MD5 hexdigest (32 chars)
    """
    str_repr = "|".join(["%s:%s" % x for x in sorted(dct.items())])
    return hashlib.md5(str_repr.encode("utf-8")).hexdigest()


def get_driver(drv_name, drv_conf):
    key = "{}.{}".format(drv_name, make_hash(drv_conf))
    if key not in CACHE:
        CACHE[key] = driver.get_driver(drv_name, drv_conf)
    return CACHE[key]


@bp.route("/notify/<backends>", methods=["POST"])
def send_notification(backends):
    global CACHE

    payload = flask.request.get_json(force=True, silent=True)

    if not payload:
        return flask.jsonify({"error": "Missed Payload"}), 400

    try:
        driver.Driver.validate_payload(payload)
    except ValueError as e:
        return flask.jsonify({"error": "Bad Payload: {}".format(e)}), 400

    backends = set(backends.split(","))
    notify_backends = config.get_config()["notify_backends"]

    unexpected = backends - set(notify_backends)
    if unexpected:
        mesg = "Unexpected backends: {}".format(", ".join(unexpected))
        return flask.jsonify({"error": mesg}), 400

    result = {"payload": payload, "result": {},
              "total": 0, "passed": 0, "failed": 0, "errors": 0}

    for backend in backends:
        for drv_name, drv_conf in notify_backends[backend].items():
            driver_ins = get_driver(drv_name, drv_conf)

            result["total"] += 1
            if backend not in result["result"]:
                result["result"][backend] = {}

            # TODO(maretskiy): run in parallel
            try:
                status = driver_ins.notify(payload)

                result["passed"] += status
                result["failed"] += not status
                result["result"][backend][drv_name] = {"status": status}
            except driver.ExplainedError as e:
                result["result"][backend][drv_name] = {"error": str(e)}
                result["errors"] += 1
            except Exception as e:
                LOG.error("Backend '{}' driver '{}': {}: {}".format(
                    backend, drv_name, type(e), e))
                error = "Something has went wrong!"
                result["result"][backend][drv_name] = {"error": error}
                result["errors"] += 1

    return flask.jsonify(result), 200


def _convert_prometheus_payload(ppayload):
    alerts = ppayload['alerts']
    converted_payloads = []
    for alert in alerts:
        converted_payloads.append({
            'region': alert['labels']['region'],
            'serverity': alert['labels']['severity'],
            'description': alert['description'],
            'who': 'Prometheus',
            'what': alert['summary'],
            'affected_hosts': alert['labels']['affected_hosts'],
        })

    return converted_payloads


@bp.route("/prometheus_notify/<backends>", methods=["POST"])
def send_prometheus_notification(backends):
    global CACHE

    prometheus_payload = flask.request.get_json(force=True, silent=True)

    if not prometheus_payload:
        return flask.jsonify({"error": "Missed Payload"}), 400

    try:
        driver.Driver.validate_prometheus_payload(prometheus_payload)
    except ValueError as e:
        return flask.jsonify({"error": "Bad Payload: {}".format(e)}), 400

    payloads = _convert_prometheus_payload(prometheus_payload)

    backends = set(backends.split(","))

    notify_backends = config.get_config()["notify_backends"]

    unexpected = backends - set(notify_backends)
    if unexpected:
        mesg = "Unexpected backends: {}".format(", ".join(unexpected))
        return flask.jsonify({"error": mesg}), 400

    errors = []
    for payload in payloads:
        for backend in backends:
            for drv_name, drv_conf in notify_backends[backend].items():
                driver_ins = get_driver(drv_name, drv_conf)

                try:
                    driver_ins.notify(payload)
                except Exception as e:
                    error = "Backend '{}' driver '{}': {}: {}".format(
                        backend, drv_name, type(e), e)
                    errors.append(error)
                    LOG.exception(error)
    if errors:
        # kszukielojc: sending 5xx code will lead to retry from prometheus
        return flask.jsonify({"error", errors}), 400

    return flask.jsonify({}), 200


def get_blueprints():
    return [["", bp]]
