"""EMQX HTTPS ingestion: authenticate, validate, then acknowledge durable storage."""
import hmac
import os

from flask import current_app, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from hardware import decode_payload, ingest, SyntheticObservationError
from app import db


def register(app):
    @app.post('/api/hardware/ingest')
    def webhook():
        token=os.getenv('HARDWARE_WEBHOOK_TOKEN')
        if not token:
            return jsonify(error='Hardware webhook is not configured'),503
        supplied=request.headers.get('Authorization','').encode('utf-8')
        if not hmac.compare_digest(supplied,('Bearer '+token).encode('utf-8')):
            return jsonify(error='Unauthorized'),401
        if request.mimetype!='application/json':
            return jsonify(error='Content-Type must be application/json'),415
        try:
            # Bound chunked requests too; do not buffer an arbitrary body first.
            request.max_content_length=16384
            envelope=decode_payload(request.get_data())
            if not isinstance(envelope,dict):raise ValueError('Expected EMQX JSON envelope')
            payload=envelope.get('payload')
            if isinstance(payload,str):payload=decode_payload(payload.encode('utf-8'))
            if not isinstance(payload,dict):raise ValueError('payload must be a JSON object')
            device=payload.get('device_id')
            if not isinstance(device,str):raise ValueError('device_id is required')
            template=os.getenv('MQTT_TOPIC_TEMPLATE','aqi/{device_id}/readings')
            if envelope.get('topic')!=template.format(device_id=device):
                raise ValueError('Topic does not match device')
            if envelope.get('username')!=os.getenv('MQTT_PUBLISH_USERNAME','mandalay_esp32'):
                raise ValueError('Unexpected broker publisher')
            retained=envelope.get('retained',False)
            if not isinstance(retained,bool):raise ValueError('retained must be a boolean')
            # Test rejection precedes message ID validation; never store tests.
            for flag in ('test','synthetic'):
                if payload.get(flag) is True:raise SyntheticObservationError('Synthetic message')
            if not isinstance(payload.get('message_id'),str) or not 1<=len(payload['message_id'])<=100:
                raise ValueError('A stable message_id of 1–100 characters is required')
            result=ingest(payload,retained=retained)
        except RequestEntityTooLarge:
            return jsonify(error='Sensor envelope exceeds 16 KiB'),413
        except SyntheticObservationError:
            # Deliberate discard is acknowledged so EMQX does not retry tests.
            return jsonify(status='rejected',reason='synthetic_message',stored=False),200
        except (ValueError,UnicodeError,BadRequest):
            return jsonify(error='Invalid Mandalay hardware envelope'),422
        except SQLAlchemyError as error:
            db.session.rollback()
            current_app.logger.error('Hardware webhook storage failed error_type=%s',type(error).__name__)
            response=jsonify(error='Hardware storage temporarily unavailable')
            response.status_code=503;response.headers['Retry-After']='10'
            return response
        # ingest commits before replying. Retrying after a lost HTTP response is safe.
        return jsonify(status='stored' if result['inserted'] else 'duplicate',
                       stored=result['inserted'],device_id=result['device_id']),201 if result['inserted'] else 200
