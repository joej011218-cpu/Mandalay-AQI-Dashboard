"""Run once as a Render background worker, never in Gunicorn workers."""
import json
import logging
import os
import signal
import ssl
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from app import create_app, db
from hardware import settings, ingest, MQTTState, HardwareObservation

log=logging.getLogger('mandalay.mqtt')
LEADER_LOCK=219588960892


def decode(payload):
    if len(payload)>16384:raise ValueError('MQTT payload exceeds 16 KiB')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate JSON fields')
            result[key]=value
        return result
    def invalid_constant(value):raise ValueError('Nonfinite JSON constants are forbidden')
    return json.loads(payload.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid_constant)


class Subscriber:
    def __init__(self,app):
        self.app=app;self.stop=threading.Event();self.connected=False
        self.pending_subscriptions={};self.authorized_topics=set()
        with app.app_context():
            cfg=settings()
            if not cfg['devices']:raise ValueError('MANDALAY_DEVICE_IDS is required')
            if not cfg['primary']:raise ValueError('HARDWARE_DEVICE_ID is required for multi-device installations')
            self.devices=cfg['devices']
        self.host=os.getenv('MQTT_HOST','');self.port=int(os.getenv('MQTT_PORT','8883'))
        if not self.host:raise ValueError('MQTT_HOST is required; no public-broker default is selected')
        if not 1<=self.port<=65535:raise ValueError('MQTT_PORT must be 1–65535')
        if self.host.lower().rstrip('.')=='broker.hivemq.com':raise ValueError('The shared public HiveMQ broker is not accepted as authenticated device transport')
        template=os.getenv('MQTT_TOPIC_TEMPLATE','aqi/{device_id}/readings')
        if any(c in template for c in ('#','+')):raise ValueError('Only exact device topics are allowed')
        if '{device_id}' not in template and len(self.devices)!=1:raise ValueError('Multi-device topics must contain {device_id}')
        self.topics={template.format(device_id=d):d for d in self.devices}
        self.client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id=os.getenv('MQTT_CLIENT_ID','mandalay-dashboard-subscriber'),
                                clean_session=False,manual_ack=True)
        username=os.getenv('MQTT_USERNAME');password=os.getenv('MQTT_PASSWORD')
        expected_username=os.getenv('MQTT_EXPECTED_USERNAME')
        if expected_username and username!=expected_username:
            raise ValueError('MQTT_USERNAME must match the configured subscriber account MQTT_EXPECTED_USERNAME')
        cert=os.getenv('MQTT_CLIENT_CERT');key=os.getenv('MQTT_CLIENT_KEY')
        insecure=os.getenv('MQTT_ALLOW_INSECURE_DEV')=='true'
        if insecure:
            if self.host not in ('localhost','127.0.0.1'):raise ValueError('Insecure MQTT is permitted only on local loopback for development')
        else:
            if not ((username and password) or (cert and key)):raise ValueError('Configure authenticated TLS MQTT credentials or a client certificate')
            context=ssl.create_default_context(cafile=os.getenv('MQTT_CA_FILE') or None)
            context.minimum_version=ssl.TLSVersion.TLSv1_2
            if cert and key:context.load_cert_chain(cert,key)
            self.client.tls_set_context(context)
        if username and password:self.client.username_pw_set(username,password)
        self.client.reconnect_delay_set(min_delay=1,max_delay=60)
        self.client.on_connect=self.on_connect;self.client.on_disconnect=self.on_disconnect;self.client.on_message=self.on_message
        self.client.on_subscribe=self.on_subscribe;self.client.on_connect_fail=self.on_connect_fail

    def save_state(self):
        with self.app.app_context():
            stmt=insert(MQTTState).values(name='subscriber',connected=self.connected,heartbeat_at=datetime.now(timezone.utc))
            db.session.execute(stmt.on_conflict_do_update(index_elements=['name'],set_={'connected':stmt.excluded.connected,'heartbeat_at':stmt.excluded.heartbeat_at}))
            db.session.commit()

    def persist_state(self):
        try:self.save_state()
        except Exception:log.error('Could not persist MQTT connection/subscription state')

    def on_connect(self,client,userdata,flags,reason_code,properties):
        self.connected=False;self.pending_subscriptions.clear();self.authorized_topics.clear()
        if reason_code==0:
            for topic in self.topics:
                result,mid=client.subscribe(topic,qos=1)
                if result==mqtt.MQTT_ERR_SUCCESS:self.pending_subscriptions[mid]=topic
                else:log.error('Could not send MQTT subscribe request; reconnect or check broker limits')
            log.info('MQTT transport authenticated; waiting for subscription authorization')
        else:
            log.error('MQTT connection refused (%s); verify subscriber credentials and deployment access',reason_code)
        self.persist_state()

    def on_subscribe(self,client,userdata,mid,reason_codes,properties):
        topic=self.pending_subscriptions.pop(mid,None)
        if topic is None:return
        codes=[getattr(code,'value',code) for code in reason_codes]
        if len(codes)!=1 or codes[0] not in (0,1):
            self.authorized_topics.discard(topic)
            log.error('MQTT subscription denied or unexpected grant; allow subscriber access to the exact device topic and restart worker')
        else:
            self.authorized_topics.add(topic)
            log.info('MQTT subscription authorized for configured device topic (QoS %s)',codes[0])
        self.connected=len(self.authorized_topics)==len(self.topics)
        self.persist_state()

    def on_connect_fail(self,client,userdata):
        self.connected=False;self.authorized_topics.clear();self.pending_subscriptions.clear()
        self.persist_state()
        log.warning('MQTT connection attempt failed; checking hostname, TLS trust or reachability may be necessary; retrying with backoff')

    def on_disconnect(self,client,userdata,flags,reason_code,properties):
        self.connected=False;self.authorized_topics.clear();self.pending_subscriptions.clear()
        self.persist_state()
        log.warning('MQTT disconnected; automatic backoff/reconnection enabled')

    def on_message(self,client,userdata,message):
        try:
            payload=decode(message.payload)
            if not isinstance(payload,dict) or self.topics.get(message.topic)!=payload.get('device_id'):raise ValueError('Device and authorized topic do not match')
            with self.app.app_context():ingest(payload,retained=bool(message.retain))
        except (ValueError,UnicodeError,json.JSONDecodeError):
            log.warning('Rejected invalid Mandalay sensor payload')
            # Poison messages should not retry forever; do not log their content.
        except Exception:
            log.error('Sensor persistence failed; reconnecting without acknowledging')
            self.client.disconnect()
            self.client.connect_async(self.host,self.port,keepalive=60)
            return
        if message.qos:client.ack(message.mid,message.qos)  # Only after durable commit or deliberate rejection.

    def run(self):
        # A session-level DB lock survives across ingestion transactions, and
        # prevents duplicate subscribers during Render rolling deployments.
        with self.app.app_context():
            conn=db.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
            try:
                while not self.stop.is_set():
                    try:
                        conn.execute(select(HardwareObservation.id).limit(1))
                        if conn.execute(text('SELECT pg_try_advisory_lock(:lock)'),{'lock':LEADER_LOCK}).scalar():break
                    except Exception:
                        conn.close();conn=db.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
                        log.warning('Waiting for additive hardware schema or database')
                    self.stop.wait(5)
                if self.stop.is_set():return
                self.client.connect_async(self.host,self.port,keepalive=60);self.client.loop_start()
                while not self.stop.wait(30):
                    conn.execute(text('SELECT 1'))  # Exit if leader connection/lock is lost.
                    self.save_state()
            finally:
                self.client.disconnect();self.client.loop_stop()
                self.connected=False
                try:self.save_state()
                except Exception:log.warning('Could not persist shutdown state')
                conn.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    app=create_app();subscriber=Subscriber(app)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:subscriber.stop.set())
    subscriber.run()
