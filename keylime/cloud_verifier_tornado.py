#!/usr/bin/python3
'''
DISTRIBUTION STATEMENT A. Approved for public release: distribution unlimited.

This material is based upon work supported by the Assistant Secretary of Defense for
Research and Engineering under Air Force Contract No. FA8721-05-C-0002 and/or
FA8702-15-D-0001. Any opinions, findings, conclusions or recommendations expressed in this
material are those of the author(s) and do not necessarily reflect the views of the
Assistant Secretary of Defense for Research and Engineering.

Copyright 2015 Massachusetts Institute of Technology.

The software/firmware is provided to you on an As-Is basis

Delivered to the US Government with Unlimited Rights, as defined in DFARS Part
252.227-7013 or 7014 (Feb 2014). Notwithstanding any copyright notice, U.S. Government
rights in this work are defined by DFARS 252.227-7013 or DFARS 252.227-7014 as detailed
above. Use of this work other than as specifically authorized by the U.S. Government may
violate any copyrights that exist in this work.
'''
import configparser
import datetime
import traceback
import os
import sys
import functools
import asyncio
import tornado.ioloop
import tornado.web
import jwt
from tornado import httpserver
from tornado.httpclient import AsyncHTTPClient
from tornado.httputil import url_concat
import keylime.tornado_requests as tornado_requests

from keylime import common
from keylime import keylime_logging
from keylime import cloud_verifier_common
from keylime import revocation_notifier
from keylime.keylime_auth import jwtauth
from keylime.crypto import get_random_string
from werkzeug.security import generate_password_hash, check_password_hash

# Database imports
from keylime.verifier_db import VerfierMain, User
from keylime.keylime_database import SessionManager
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine
from sqlalchemy.engine.url import URL

logger = keylime_logging.init_logging('cloudverifier')

SECRET = 'my_secret_key'
PREFIX = 'Bearer '

try:
    import simplejson as json
except ImportError:
    raise("Simplejson is mandatory, please install")

if sys.version_info[0] < 3:
    raise Exception("Python 3 or a more recent version is required.")

config = configparser.ConfigParser()
config.read(common.CONFIG_FILE)

drivername = config.get('cloud_verifier', 'drivername')

if drivername == 'sqlite':
    database = "%s/%s" % (common.WORK_DIR,
                          config.get('cloud_verifier', 'database'))
    url = URL(
        drivername=drivername,
        username='',
        password='',
        host='',
        database=(database)
    )
else:
    url = URL(
        drivername=drivername,
        username=config.get('cloud_verifier', 'username'),
        password=config.get('cloud_verifier', 'password'),
        host=config.get('cloud_verifier', 'host'),
        database=config.get('cloud_verifier', 'database')
    )


try:
    engine = create_engine(url,
                           connect_args={'check_same_thread': False},)
except SQLAlchemyError as e:
    logger.error(f'Error creating SQL engine: {e}')
    exit(1)



# The "exclude_db" dict values are removed from the response before adding the dict to the DB
# This is because we want these values to remain ephemeral and not stored in the database.
exclude_db = {
    'registrar_keys': '',
    'nonce': '',
    'b64_encrypted_V': '',
    'provide_V': True,
    'num_retries': 0,
    'pending_event': None,
    'first_verified': False,
}


class BaseHandler(tornado.web.RequestHandler, SessionManager):
    def prepare(self):
        super(BaseHandler, self).prepare()

    def write_error(self, status_code, **kwargs):

        self.set_header('Content-Type', 'text/json')
        if self.settings.get("serve_traceback") and "exc_info" in kwargs:
            # in debug mode, try to send a traceback
            lines = []
            for line in traceback.format_exception(*kwargs["exc_info"]):
                lines.append(line)
            self.finish(json.dumps({
                'code': status_code,
                'status': self._reason,
                'traceback': lines,
                'results': {},
            }))
        else:
            self.finish(json.dumps({
                'code': status_code,
                'status': self._reason,
                'results': {},
            }))


class MainHandler(tornado.web.RequestHandler):

    def head(self):
        common.echo_json_response(
            self, 405, "Not Implemented: Use /agents/ interface instead")

    def get(self):
        common.echo_json_response(
            self, 405, "Not Implemented: Use /agents/ interface instead")

    def delete(self):
        common.echo_json_response(
            self, 405, "Not Implemented: Use /agents/ interface instead")

    def post(self):
        common.echo_json_response(
            self, 405, "Not Implemented: Use /agents/ interface instead")

    def put(self):
        common.echo_json_response(
            self, 405, "Not Implemented: Use /agents/ interface instead")


class AuthHandler(BaseHandler):
    """
        Handle to auth method.
        This method aim to provide a new authorization token
        There is a fake payload (for tutorial purpose)
    """

    def prepare(self):
        """
            Encode a new token with JSON Web Token (PyJWT)
        """

    def get(self, *args, **kwargs):
        session = self.make_session(engine)
        """
            return the generated token
        """
        username = self.get_argument("username")
        password = self.get_argument("password")

        user = session.query(User).filter(User.username == username).first()
        if user is not None and user.username == username:
            if check_password_hash(user.password, password):
                logger.info(f"User {username} authorized")
                encoded = jwt.encode({
                    'group_id': user.group_id,
                    'role_id': user.role_id,
                    'exp': datetime.datetime.utcnow() + datetime.timedelta(seconds=600)},
                    SECRET,
                    'HS256'
                ).decode('utf-8')
                response = {'token': encoded}
                common.echo_json_response(
                            self, 200, response)
            else:
                logger.error(f"User {username} authorization failed")
                common.echo_json_response(
                            self, 401, "User %s authorization failed" % (username))
        else:
            logger.error(f"User {username} account not found")
            common.echo_json_response(
                            self, 404, "User account %s not found" % (username))


@jwtauth
class RegisterHandler(BaseHandler):
    """
        User registration, should only be allowed by admin
    """

    def post(self):
        session = self.make_session(engine)
        # Get the new user details.
        new_username = self.get_argument("username")
        new_password = self.get_argument("password")
        new_email = self.get_argument("email")
        new_group_id = self.get_argument("group_id")
        token = self.request.headers.get("Authorization")[len(PREFIX):]
        payload = jwt.decode(token, SECRET, algorithms='HS256')

        try:
            user = session.query(User).filter(User.username == new_username).first()
        except SQLAlchemyError as e:
            logger.error(f'SQLAlchemy Error: {e}')

        if payload['role_id'] == 1:
            if user is not None and user.username == new_username:
                logger.error(f"Username {new_username} already exists")
                common.echo_json_response(
                            self, 409, "User %s already exists" % (new_username))
            else:
                try:
                    new_user = User(username=new_username, password=generate_password_hash(new_password), email=new_email, group_id=new_group_id)
                    session.add(new_user)
                    session.commit()
                except SQLAlchemyError as e:
                    logger.error(f'SQLAlchemy Error: {e}')
                    common.echo_json_response(
                                self, 500, "Internal server error %s " % (e))
                logger.info(f"Username {new_username} registered successfully")
                common.echo_json_response(
                            self, 200, "Username %s registered successfully" % (new_username))
        else:
            logger.info(f"Only admin users are authorized to add new users")
            common.echo_json_response(
                            self, 409, "Only admin users are authorized to add new users")

@jwtauth
class AgentsHandler(BaseHandler):
    def head(self):
        """HEAD not supported"""
        common.echo_json_response(self, 405, "HEAD not supported")

    def get(self):
        """This method handles the GET requests to retrieve status on agents from the Cloud Verifier.

        Currently, only agents resources are available for GETing, i.e. /agents. All other GET uri's
        will return errors. Agents requests require a single agent_id parameter which identifies the
        agent to be returned. If the agent_id is not found, a 404 response is returned.  If the agent_id
        was not found, it either completed successfully, or failed.  If found, the agent_id is still polling
        to contact the Cloud Agent.
        """
        session = self.make_session(engine)
        rest_params = common.get_restful_params(self.request.uri)
        if rest_params is None:
            common.echo_json_response(
                self, 405, "Not Implemented: Use /agents/ interface")
            return

        if "agents" not in rest_params:
            common.echo_json_response(self, 400, "uri not supported")
            logger.warning(
                'GET returning 400 response. uri not supported: ' + self.request.path)
            return

        agent_id = rest_params["agents"]

        if agent_id is not None:
            agent = session.query(VerfierMain).filter_by(agent_id=agent_id).one()

            if agent is not None:
                response = cloud_verifier_common.process_get_status(agent)
                common.echo_json_response(self, 200, "Success", response)
            else:
                common.echo_json_response(self, 404, "agent id not found")
        else:
            json_response = session.query(VerfierMain.agent_id).all()
            common.echo_json_response(self, 200, "Success", {
                'uuids': json_response})
            logger.info('GET returning 200 response for agent_id list')

    def delete(self):
        """This method handles the DELETE requests to remove agents from the Cloud Verifier.

        Currently, only agents resources are available for DELETEing, i.e. /agents. All other DELETE uri's will return errors.
        agents requests require a single agent_id parameter which identifies the agent to be deleted.
        """
        session = self.make_session(engine)
        rest_params = common.get_restful_params(self.request.uri)
        if rest_params is None:
            common.echo_json_response(
                self, 405, "Not Implemented: Use /agents/ interface")
            return

        if "agents" not in rest_params:
            common.echo_json_response(self, 400, "uri not supported")
            return

        agent_id = rest_params["agents"]

        if agent_id is None:
            common.echo_json_response(self, 400, "uri not supported")
            logger.warning(
                'DELETE returning 400 response. uri not supported: ' + self.request.path)

        agent = session.query(VerfierMain).filter_by(agent_id=agent_id).first()

        if agent is None:
            common.echo_json_response(self, 404, "agent id not found")
            logger.info('DELETE returning 404 response. agent id: ' +
                        agent_id + ' not found.')
            return

        op_state = agent.operational_state
        if op_state == cloud_verifier_common.CloudAgent_Operational_State.SAVED or \
                op_state == cloud_verifier_common.CloudAgent_Operational_State.FAILED or \
                op_state == cloud_verifier_common.CloudAgent_Operational_State.TERMINATED or \
                op_state == cloud_verifier_common.CloudAgent_Operational_State.TENANT_FAILED or \
                op_state == cloud_verifier_common.CloudAgent_Operational_State.INVALID_QUOTE:
            session.query(VerfierMain).filter_by(agent_id=agent_id).delete()
            session.commit()
            common.echo_json_response(self, 200, "Success")
            logger.info(
                'DELETE returning 200 response for agent id: ' + agent_id)
        else:
            try:
                update_agent = session.query(VerfierMain).get(agent_id)
                update_agent.operational_state = cloud_verifier_common.CloudAgent_Operational_State.TERMINATED
                session.add(update_agent)
                session.commit()
                common.echo_json_response(self, 202, "Accepted")
                logger.info(
                    'DELETE returning 202 response for agent id: ' + agent_id)
            except SQLAlchemyError as e:
                logger.error(f'SQLAlchemy Error: {e}')

    def post(self):
        session = self.make_session(engine)
        """This method handles the POST requests to add agents to the Cloud Verifier.

        Currently, only agents resources are available for POSTing, i.e. /agents. All other POST uri's will return errors.
        agents requests require a json block sent in the body
        """
        try:
            rest_params = common.get_restful_params(self.request.uri)
            if rest_params is None:
                common.echo_json_response(
                    self, 405, "Not Implemented: Use /agents/ interface")
                return

            if "agents" not in rest_params:
                common.echo_json_response(self, 400, "uri not supported")
                logger.warning(
                    'POST returning 400 response. uri not supported: ' + self.request.path)
                return

            agent_id = rest_params["agents"]

            if agent_id is not None:
                content_length = len(self.request.body)
                if content_length == 0:
                    common.echo_json_response(
                        self, 400, "Expected non zero content length")
                    logger.warning(
                        'POST returning 400 response. Expected non zero content length.')
                else:
                    json_body = json.loads(self.request.body)
                    agent_data = {}
                    agent_data['v'] = json_body['v']
                    agent_data['ip'] = json_body['cloudagent_ip']
                    agent_data['port'] = int(json_body['cloudagent_port'])
                    agent_data['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.START
                    agent_data['public_key'] = ""
                    agent_data['tpm_policy'] = json_body['tpm_policy']
                    agent_data['vtpm_policy'] = json_body['vtpm_policy']
                    agent_data['meta_data'] = json_body['metadata']
                    agent_data['ima_whitelist'] = json_body['ima_whitelist']
                    agent_data['revocation_key'] = json_body['revocation_key']
                    agent_data['tpm_version'] = 0
                    agent_data['accept_tpm_hash_algs'] = json_body['accept_tpm_hash_algs']
                    agent_data['accept_tpm_encryption_algs'] = json_body['accept_tpm_encryption_algs']
                    agent_data['accept_tpm_signing_algs'] = json_body['accept_tpm_signing_algs']
                    agent_data['hash_alg'] = ""
                    agent_data['enc_alg'] = ""
                    agent_data['sign_alg'] = ""
                    agent_data['agent_id'] = agent_id

                    try:
                        new_agent_count = session.query(
                            VerfierMain).filter_by(agent_id=agent_id).count()
                    except SQLAlchemyError as e:
                        logger.error(f'SQLAlchemy Error: {e}')

                    # don't allow overwriting

                    if new_agent_count > 0:
                        common.echo_json_response(
                            self, 409, "Agent of uuid %s already exists" % (agent_id))
                        logger.warning(
                            "Agent of uuid %s already exists" % (agent_id))
                    else:
                        try:
                            # Add the agent and data
                            session.add(VerfierMain(**agent_data))
                            session.commit()
                        except SQLAlchemyError as e:
                            logger.error(f'SQLAlchemy Error: {e}')

                        for key in list(exclude_db.keys()):
                            agent_data[key] = exclude_db[key]
                        asyncio.ensure_future(self.process_agent(
                            agent_data, cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE))
                        common.echo_json_response(self, 200, "Success")
                        logger.info(
                            'POST returning 200 response for adding agent id: ' + agent_id)
            else:
                common.echo_json_response(self, 400, "uri not supported")
                logger.warning(
                    "POST returning 400 response. uri not supported")
        except Exception as e:
            common.echo_json_response(self, 400, "Exception error: %s" % e)
            logger.warning(
                "POST returning 400 response. Exception error: %s" % e)
            logger.exception(e)

        self.finish()

    def put(self):
        session = self.make_session(engine)
        """This method handles the PUT requests to add agents to the Cloud Verifier.

        Currently, only agents resources are available for PUTing, i.e. /agents. All other PUT uri's will return errors.
        agents requests require a json block sent in the body
        """
        try:
            rest_params = common.get_restful_params(self.request.uri)
            if rest_params is None:
                common.echo_json_response(
                    self, 405, "Not Implemented: Use /agents/ interface")
                return

            if "agents" not in rest_params:
                common.echo_json_response(self, 400, "uri not supported")
                logger.warning(
                    'PUT returning 400 response. uri not supported: ' + self.request.path)
                return

            agent_id = rest_params["agents"]
            if agent_id is None:
                common.echo_json_response(self, 400, "uri not supported")
                logger.warning("PUT returning 400 response. uri not supported")
            try:
                agent = session.query(VerfierMain).filter_by(agent_id=agent_id)
            except SQLAlchemyError as e:
                logger.error(f'SQLAlchemy Error: {e}')

            if agent is not None:
                common.echo_json_response(self, 404, "agent id not found")
                logger.info(
                    'PUT returning 404 response. agent id: ' + agent_id + ' not found.')

            if "reactivate" in rest_params:
                agent['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.START
                asyncio.ensure_future(self.process_agent(
                    agent, cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE))
                common.echo_json_response(self, 200, "Success")
                logger.info(
                    'PUT returning 200 response for agent id: ' + agent_id)
            elif "stop" in rest_params:
                # do stuff for terminate
                logger.debug("Stopping polling on %s" % agent_id)
                try:
                    session.query(VerfierMain).filter(agent_id == agent_id).update(
                        {'operational_state': cloud_verifier_common.CloudAgent_Operational_State.TENANT_FAILED})
                    session.commit()
                except SQLAlchemyError as e:
                    logger.error(f'SQLAlchemy Error: {e}')

                common.echo_json_response(self, 200, "Success")
                logger.info(
                    'PUT returning 200 response for agent id: ' + agent_id)
            else:
                common.echo_json_response(self, 400, "uri not supported")
                logger.warning("PUT returning 400 response. uri not supported")

        except Exception as e:
            common.echo_json_response(self, 400, "Exception error: %s" % e)
            logger.warning(
                "PUT returning 400 response. Exception error: %s" % e)
            logger.exception(e)
        self.finish()

    async def invoke_get_quote(self, agent, need_pubkey):
        if agent is None:
            raise Exception("agent deleted while being processed")
        params = cloud_verifier_common.prepare_get_quote(agent)

        partial_req = "1"
        if need_pubkey:
            partial_req = "0"

        res = tornado_requests.request("GET",
                                       "http://%s:%d/quotes/integrity?nonce=%s&mask=%s&vmask=%s&partial=%s" %
                                       (agent['ip'], agent['port'], params["nonce"], params["mask"], params['vmask'], partial_req), context=None)
        response = await res

        if response.status_code != 200:
            # this is a connection error, retry get quote
            if response.status_code == 599:
                asyncio.ensure_future(self.process_agent(
                    agent, cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE_RETRY))
            else:
                # catastrophic error, do not continue
                error = "Unexpected Get Quote response error for cloud agent " + \
                    agent['agent_id'] + ", Error: " + str(response.status_code)
                logger.critical(error)
                asyncio.ensure_future(self.process_agent(
                    agent, cloud_verifier_common.CloudAgent_Operational_State.FAILED))
        else:
            try:
                json_response = json.loads(response.body)

                # validate the cloud agent response
                if cloud_verifier_common.process_quote_response(agent, json_response['results']):
                    if agent['provide_V']:
                        asyncio.ensure_future(self.process_agent(
                            agent, cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V))
                    else:
                        asyncio.ensure_future(self.process_agent(
                            agent, cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE))
                else:
                    asyncio.ensure_future(self.process_agent(
                        agent, cloud_verifier_common.CloudAgent_Operational_State.INVALID_QUOTE))

            except Exception as e:
                logger.exception(e)

    async def invoke_provide_v(self, agent):
        if agent is None:
            raise Exception("Agent deleted while being processed")
        try:
            if agent['pending_event'] is not None:
                agent['pending_event'] = None
        except KeyError:
            pass
        v_json_message = cloud_verifier_common.prepare_v(agent)
        res = tornado_requests.request(
            "POST", "http://%s:%d//keys/vkey" % (agent['ip'], agent['port']), data=v_json_message)
        response = await res

        if response.status_code != 200:
            if response.status_code == 599:
                asyncio.ensure_future(self.process_agent(
                    agent, cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V_RETRY))
            else:
                # catastrophic error, do not continue
                error = "Unexpected Provide V response error for cloud agent " + \
                    agent['agent_id'] + ", Error: " + str(response.error)
                logger.critical(error)
                asyncio.ensure_future(self.process_agent(
                    agent, cloud_verifier_common.CloudAgent_Operational_State.FAILED))
        else:
            asyncio.ensure_future(self.process_agent(
                agent, cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE))

    async def process_agent(self, agent, new_operational_state):
        session = self.make_session(engine)
        try:
            main_agent_operational_state = agent['operational_state']
            try:
                stored_agent = session.query(VerfierMain).filter_by(
                    agent_id=str(agent['agent_id'])).first()
            except SQLAlchemyError as e:
                logger.error(f'SQLAlchemy Error: {e}')

            # if the user did terminated this agent
            if stored_agent.operational_state == cloud_verifier_common.CloudAgent_Operational_State.TERMINATED:
                logger.warning("agent %s terminated by user." %
                               agent['agent_id'])
                if agent['pending_event'] is not None:
                    tornado.ioloop.IOLoop.current().remove_timeout(
                        agent['pending_event'])
                session.query(VerfierMain).filter_by(
                    agent_id=agent['agent_id']).delete()
                session.commit()
                return

            # if the user tells us to stop polling because the tenant quote check failed
            if stored_agent.operational_state == cloud_verifier_common.CloudAgent_Operational_State.TENANT_FAILED:
                logger.warning(
                    "agent %s has failed tenant quote.  stopping polling" % agent['agent_id'])
                if agent['pending_event'] is not None:
                    tornado.ioloop.IOLoop.current().remove_timeout(
                        agent['pending_event'])
                return

            # If failed during processing, log regardless and drop it on the floor
            # The administration application (tenant) can GET the status and act accordingly (delete/retry/etc).
            if new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.FAILED or \
                    new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.INVALID_QUOTE:
                agent['operational_state'] = new_operational_state

                # issue notification for invalid quotes
                if new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.INVALID_QUOTE:
                    cloud_verifier_common.notifyError(agent)

                if agent['pending_event'] is not None:
                    tornado.ioloop.IOLoop.current().remove_timeout(
                        agent['pending_event'])
                for key in exclude_db:
                    if key in agent:
                        del agent[key]
                session.query(VerfierMain).filter_by(
                    agent_id=agent['agent_id']).update(agent)
                session.commit()

                logger.warning("agent %s failed, stopping polling" %
                               agent['agent_id'])
                return

            # propagate all state, but remove none DB keys first (using exclude_db)
            try:
                agent_db = dict(agent)
                for key in exclude_db:
                    if key in agent_db:
                        del agent_db[key]

                session.query(VerfierMain).filter_by(
                    agent_id=agent_db['agent_id']).update(agent_db)
                session.commit()
            except SQLAlchemyError as e:
                logger.error(f'SQLAlchemy Error: {e}')

            # if new, get a quote
            if main_agent_operational_state == cloud_verifier_common.CloudAgent_Operational_State.START and \
                    new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE:
                agent['num_retries'] = 0
                agent['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE
                await self.invoke_get_quote(agent, True)
                return

            if main_agent_operational_state == cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE and \
                    (new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V):
                agent['num_retries'] = 0
                agent['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V
                await self.invoke_provide_v(agent)
                return

            if (main_agent_operational_state == cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V or
                    main_agent_operational_state == cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE) and \
                    new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE:
                agent['num_retries'] = 0
                interval = config.getfloat('cloud_verifier', 'quote_interval')
                agent['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE
                if interval == 0:
                    await self.invoke_get_quote(agent, False)
                else:
                    logger.debug(
                        "Setting up callback to check again in %f seconds" % interval)
                    # set up a call back to check again
                    cb = functools.partial(self.invoke_get_quote, agent, False)
                    pending = tornado.ioloop.IOLoop.current().call_later(interval, cb)
                    agent['pending_event'] = pending
                return

            maxr = config.getint('cloud_verifier', 'max_retries')
            retry = config.getfloat('cloud_verifier', 'retry_interval')
            if main_agent_operational_state == cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE and \
                    new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE_RETRY:
                if agent['num_retries'] >= maxr:
                    logger.warning("agent %s was not reachable for quote in %d tries, setting state to FAILED" % (
                        agent['agent_id'], maxr))
                    if agent['first_verified']:  # only notify on previously good agents
                        cloud_verifier_common.notifyError(agent, 'comm_error')
                    else:
                        logger.debug(
                            "Communication error for new agent.  no notification will be sent")
                    await self.process_agent(agent, cloud_verifier_common.CloudAgent_Operational_State.FAILED)
                else:
                    agent['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.GET_QUOTE
                    cb = functools.partial(self.invoke_get_quote, agent, True)
                    agent['num_retries'] += 1
                    logger.info("connection to %s refused after %d/%d tries, trying again in %f seconds" %
                                (agent['ip'], agent['num_retries'], maxr, retry))
                    tornado.ioloop.IOLoop.current().call_later(retry, cb)
                return

            if main_agent_operational_state == cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V and \
                    new_operational_state == cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V_RETRY:
                if agent['num_retries'] >= maxr:
                    logger.warning("agent %s was not reachable to provide v in %d tries, setting state to FAILED" % (
                        agent['agent_id'], maxr))
                    cloud_verifier_common.notifyError(agent, 'comm_error')
                    await self.process_agent(agent, cloud_verifier_common.CloudAgent_Operational_State.FAILED)
                else:
                    agent['operational_state'] = cloud_verifier_common.CloudAgent_Operational_State.PROVIDE_V
                    cb = functools.partial(self.invoke_provide_v, agent)
                    agent['num_retries'] += 1
                    logger.info("connection to %s refused after %d/%d tries, trying again in %f seconds" %
                                (agent['ip'], agent['num_retries'], maxr, retry))
                    tornado.ioloop.IOLoop.current().call_later(retry, cb)
                return
            raise Exception("nothing should ever fall out of this!")

        except Exception as e:
            logger.error("Polling thread error: %s" % e)
            logger.exception(e)


def check_user_db():
    session = SessionManager().make_session(engine)
    user = session.query(User).filter(User.username == "admin").first()
    if user is None:
        logger.info("Creating admin user")
        admin_pass = get_random_string(16)
        new_user = User(username="admin", password=generate_password_hash(admin_pass), email="admin@localhost", group_id="1", role_id="1")
        try:
            session.add(new_user)
            session.commit()
        except SQLAlchemyError as e:
            logger.error(f'SQLAlchemy Error: {e}')
        logger.warn(f"Admin password is: {admin_pass} This must be changed immediately.")


def start_tornado(tornado_server, port):
    tornado_server.listen(port)
    print("Starting Torando on port " + str(port))
    tornado.ioloop.IOLoop.instance().start()
    print("Tornado finished")


def main(argv=sys.argv):
    """Main method of the Cloud Verifier Server.  This method is encapsulated in a function for packaging to allow it to be
    called as a function by an external program."""

    config = configparser.ConfigParser()
    config.read(common.CONFIG_FILE)
    cloudverifier_port = config.get('cloud_verifier', 'cloudverifier_port')
    os.umask(0o077)
    kl_dir = os.path.dirname(os.path.abspath(database))
    if not os.path.exists(kl_dir):
        os.makedirs(kl_dir, 0o700)
    VerfierMain.metadata.create_all(engine, checkfirst=True)
    session = SessionManager().make_session(engine)
    try:
        query_all = session.query(VerfierMain).all()
    except SQLAlchemyError as e:
        logger.error(f'SQLAlchemy Error: {e}')
    for row in query_all:
        row.operational_state = cloud_verifier_common.CloudAgent_Operational_State.SAVED
    try:
        session.commit()
    except SQLAlchemyError as e:
        logger.error(f'SQLAlchemy Error: {e}')

    num = session.query(VerfierMain.agent_id).count()
    if num > 0:
        agent_ids = session.query(VerfierMain.agent_id).all()
        logger.info("agent ids in db loaded from file: %s" % agent_ids)

    check_user_db()

    logger.info('Starting Cloud Verifier (tornado) on port ' +
                cloudverifier_port + ', use <Ctrl-C> to stop')

    settings = {
        'debug': True,
        "xsrf_cookies": False,  # change me!
        }

    app = tornado.web.Application([
        (r"/(?:v[0-9]/)?agents/.*", AgentsHandler),
        (r"/auth", AuthHandler),
        (r"/register", RegisterHandler),
        (r".*", MainHandler),
    ], **settings)

    context = cloud_verifier_common.init_mtls()

    # after TLS is up, start revocation notifier
    if config.getboolean('cloud_verifier', 'revocation_notifier'):
        logger.info("Starting service for revocation notifications on port %s" %
                    config.getint('cloud_verifier', 'revocation_notifier_port'))
        revocation_notifier.start_broker()

    sockets = tornado.netutil.bind_sockets(
        int(cloudverifier_port), address='0.0.0.0')
    tornado.process.fork_processes(config.getint(
        'cloud_verifier', 'multiprocessing_pool_num_workers'))
    asyncio.set_event_loop(asyncio.new_event_loop())
    server = tornado.httpserver.HTTPServer(app, ssl_options=context)
    server.add_sockets(sockets)

    try:
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        tornado.ioloop.IOLoop.instance().stop()
        if config.getboolean('cloud_verifier', 'revocation_notifier'):
            revocation_notifier.stop_broker()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(e)
