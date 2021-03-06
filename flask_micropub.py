# -*- coding: utf-8 -*-
"""
    Flask-Micropub
    ==============

    This extension adds the ability to login to a Flask-based website
    using [IndieAuth](https://indiewebcamp.com/IndieAuth), and to request
    an [Micropub](https://indiewebcamp.com/Micropub) access token.
"""

import requests
import bs4
import flask
import functools
import uuid

import sys
if sys.version < '3':
    from urlparse import parse_qs
    from urllib import urlencode
else:
    from urllib.parse import urlencode, parse_qs

DEFAULT_AUTH_URL = 'https://indieauth.com/auth'


class MicropubClient:
    """Flask-Micropub provides support for IndieAuth/Micropub
    authentication and authorization.
    """

    def __init__(self, app=None, client_id=None):
        """Initialize the Micropub extension

        Args:
          app (flask.Flask, optional): the flask application to extend.
          client_id (string, optional): the IndieAuth client id, will be displayed
            when the user is asked to authorize this client.
        """
        self.app = app
        self.client_id = client_id
        if app is not None:
            self.init_app(app, client_id)

    def init_app(self, app, client_id=None):
        """Initialize the Micropub extension if it was not given app
        in the constructor.

        Args:
          app (flask.Flask): the flask application to extend.
          client_id (string, optional): the IndieAuth client id, will be
            displayed when the user is asked to authorize this client. If not
            provided, the app name will be used.
        """
        if not self.client_id:
            if client_id:
                self.client_id = client_id
            else:
                self.client_id = app.name

    def authenticate(self, me, state=None, next_url=None):
        """Authenticate a user via IndieAuth.

        Args:
          me (string): the authing user's URL. if it does not begin with
            https?://, http:// will be prepended.
          state (string, optional): passed through the whole auth process,
            useful if you want to maintain some state, e.g. the starting page
            to return to when auth is complete.
          next_url (string, optional): deprecated and replaced by the more
            general "state". still here for backward compatibility.

        Returns:
          a redirect to the user's specified authorization url, or
          https://indieauth.com/auth if none is provided.
        """
        redirect_url = flask.url_for(
            self.flask_endpoint_for_function(self._authenticated_handler),
            _external=True)
        return self._start_indieauth(me, redirect_url, state or next_url, None)

    def authorize(self, me, state=None, next_url=None, scope='read'):
        """Authorize a user via Micropub.

        Args:
          me (string): the authing user's URL. if it does not begin with
            https?://, http:// will be prepended.
          state (string, optional): passed through the whole auth process,
            useful if you want to maintain some state, e.g. the starting page
            to return to when auth is complete.
          next_url (string, optional): deprecated and replaced by the more
            general "state". still here for backward compatibility.
          scope (string, optional): a space-separated string of micropub
            scopes. 'read' by default.

        Returns:
          a redirect to the user's specified authorization
          https://indieauth.com/auth if none is provided.
        """
        redirect_url = flask.url_for(
            self.flask_endpoint_for_function(self._authorized_handler),
            _external=True)
        return self._start_indieauth(
            me, redirect_url, state or next_url, scope)

    def _start_indieauth(self, me, redirect_url, state, scope):
        """Helper for both authentication and authorization. Kicks off
        IndieAuth by fetching the authorization endpoint from the user's
        homepage and redirecting to it.

        Args:
          me (string): the authing user's URL. if it does not begin with
            https?://, http:// will be prepended.
          redirect_url: the callback URL that we pass to the auth endpoint.
          state (string, optional): passed through the whole auth process,
            useful if you want to maintain some state, e.g. the url to return
            to when the process is complete.
          scope (string): a space-separated string of micropub scopes.

        Returns:
          a redirect to the user's specified authorization
          https://indieauth.com/auth if none is provided.
        """

        if not me.startswith('http://') and not me.startswith('https://'):
            me = 'http://' + me
        auth_url, token_url, micropub_url = self._discover_endpoints(me)
        if not auth_url:
            auth_url = DEFAULT_AUTH_URL

        csrf_token = uuid.uuid4().hex
        flask.session['_micropub_csrf_token'] = csrf_token

        auth_params = {
            'me': me,
            'client_id': self.client_id,
            'redirect_uri': redirect_url,
            'state': '{}|{}'.format(csrf_token, state or ''),
        }
        if scope:
            auth_params['scope'] = scope

        auth_url = auth_url + '?' + urlencode(auth_params)
        flask.current_app.logger.debug('redirecting to %s', auth_url)

        return flask.redirect(auth_url)

    def authenticated_handler(self, f):
        """Decorates the authentication callback endpoint. The endpoint should
        take one argument, a flask.ext.micropub.AuthResponse.
        """
        @functools.wraps(f)
        def decorated():
            resp = self._handle_authenticate_response()
            return f(resp)
        self._authenticated_handler = decorated
        return decorated

    def authorized_handler(self, f):
        """Decorates the authorization callback endpoint. The endpoint should
        take one argument, a flask.ext.micropub.AuthResponse.
        """
        @functools.wraps(f)
        def decorated():
            resp = self._handle_authorize_response()
            return f(resp)
        self._authorized_handler = decorated
        return decorated

    def _handle_authenticate_response(self):
        code = flask.request.args.get('code')
        wrapped_state = flask.request.args.get('state')
        me = flask.request.args.get('me')
        redirect_uri = flask.url_for(flask.request.endpoint, _external=True)

        if wrapped_state and '|' in wrapped_state:
            csrf_token, state = wrapped_state.split('|', 1)
        else:
            csrf_token = state = None

        if not csrf_token:
            return AuthResponse(
                state=state, error='no CSRF token in response')

        if csrf_token != flask.session.get('_micropub_csrf_token'):
            return AuthResponse(
                state=state, error='mismatched CSRF token')

        auth_url = self._discover_endpoints(me)[0]
        if not auth_url:
            auth_url = DEFAULT_AUTH_URL

        # validate the authorization code
        auth_data = {
            'code': code,
            'client_id': self.client_id,
            'redirect_uri': redirect_uri,
            'state': wrapped_state,
        }
        flask.current_app.logger.debug(
            'Flask-Micropub: checking code against auth url: %s, data: %s',
            auth_url, auth_data)
        response = requests.post(auth_url, data=auth_data)
        flask.current_app.logger.debug(
            'Flask-Micropub: auth response: %d - %s', response.status_code,
            response.text)

        rdata = parse_qs(response.text)
        if response.status_code != 200:
            error_vals = rdata.get('error')
            error_descs = rdata.get('error_description')
            return AuthResponse(
                state=state,
                error='authorization failed. {}: {}'.format(
                    error_vals[0] if error_vals else 'Unknown Error',
                    error_descs[0] if error_descs else 'Unknown Error'))

        if 'me' not in rdata:
            return AuthResponse(
                state=state,
                error='missing "me" in response')

        confirmed_me = rdata.get('me')[0]
        return AuthResponse(me=confirmed_me, state=state)

    def _handle_authorize_response(self):
        code = flask.request.args.get('code')
        wrapped_state = flask.request.args.get('state')
        me = flask.request.args.get('me')
        redirect_uri = flask.url_for(flask.request.endpoint, _external=True)

        if wrapped_state and '|' in wrapped_state:
            csrf_token, state = wrapped_state.split('|', 1)
        else:
            csrf_token = state = None

        if not csrf_token:
            return AuthResponse(
                state=state, error='no CSRF token in response')

        if csrf_token != flask.session.get('_micropub_csrf_token'):
            return AuthResponse(
                state=state, error='mismatched CSRF token')

        token_url, micropub_url = self._discover_endpoints(me)[1:]

        if not token_url or not micropub_url:
            # successfully auth'ed user, no micropub endpoint
            return AuthResponse(
                me=me,
                state=state,
                error='no micropub endpoint found.')

        # request an access token
        token_data = {
            'code': code,
            'me': me,
            'redirect_uri': redirect_uri,
            'client_id': self.client_id,
            'state': wrapped_state,
        }
        flask.current_app.logger.debug(
            'Flask-Micropub: requesting access token from: %s, data: %s',
            token_url, token_data)
        token_response = requests.post(token_url, data=token_data)
        flask.current_app.logger.debug(
            'Flask-Micropub: token response: %d - %s',
            token_response.status_code, token_response.text)

        if token_response.status_code != 200:
            return AuthResponse(
                me=me,
                state=state,
                error='bad response from token endpoint: {}'
                .format(token_response))

        tdata = parse_qs(token_response.text)
        if 'access_token' not in tdata:
            return AuthResponse(
                me=me,
                state=state,
                error='response from token endpoint missing access_token: {}'
                .format(tdata))

        # success!
        access_token = tdata.get('access_token')[0]
        confirmed_me = tdata.get('me')[0]
        confirmed_scope = tdata.get('scope')[0]
        return AuthResponse(
            me=confirmed_me,
            micropub_endpoint=micropub_url,
            access_token=access_token,
            scope=confirmed_scope,
            state=state)

    def _discover_endpoints(self, me):
        me_response = requests.get(me)
        if me_response.status_code != 200:
            return None, None, None

        auth_endpoint = me_response.links.get('authorization_endpoint', {}).get('url')
        token_endpoint = me_response.links.get('token_endpoint', {}).get('url')
        micropub_endpoint = me_response.links.get('micropub', {}).get('url')

        if not auth_endpoint or not token_endpoint or not micropub_endpoint:
            soup = bs4.BeautifulSoup(me_response.text)
            if not auth_endpoint:
                auth_link = soup.find('link', {'rel': 'authorization_endpoint'})
                auth_endpoint = auth_link and auth_link['href']
            if not token_endpoint:
                token_link = soup.find('link', {'rel': 'token_endpoint'})
                token_endpoint = token_link and token_link['href']
            if not micropub_endpoint:
                micropub_link = soup.find('link', {'rel': 'micropub'})
                micropub_endpoint = micropub_link and micropub_link['href']

        return auth_endpoint, token_endpoint, micropub_endpoint

    @staticmethod
    def flask_endpoint_for_function(func):
        for endpt, view_func in flask.current_app.view_functions.items():
            if func == view_func:
                return endpt


class AuthResponse:
    """Authorization response, passed to the authorized_handler endpoint.

    Attributes:
      me (string): The authenticated user's URL. This will be non-None if and
        only if the user was successfully authenticated.
      micropub_endpoint (string): The endpoint to POST micropub requests to.
      access_token (string): The authorized user's micropub access token.
      state (string): The optional state that was passed to authorize.
      scope (string): The scope that comes with the micropub access token
      error (string): describes the error encountered if any. It is possible
        that the authentication step will succeed but the access token step
        will fail, in which case me will be non-None, and error will describe
        this condition.
    """
    def __init__(self, me=None, micropub_endpoint=None,
                 access_token=None, state=None, scope=None,
                 error=None):
        self.me = me
        self.micropub_endpoint = micropub_endpoint
        self.access_token = access_token
        self.next_url = self.state = state
        self.scope = scope
        self.error = error
