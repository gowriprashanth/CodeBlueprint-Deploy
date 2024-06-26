# -*- coding: utf-8 -*-

"""
requests.models
~~~~~~~~~~~~~~~

This module contains the primary objects that power Requests.
"""

import os
import socket
import collections
import logging

from datetime import datetime
from io import BytesIO

from .hooks import dispatch_hook, HOOKS
from .structures import CaseInsensitiveDict
from .status_codes import codes

from .auth import HTTPBasicAuth, HTTPProxyAuth
from .cookies import cookiejar_from_dict, extract_cookies_to_jar, get_cookie_header
from .packages.urllib3.exceptions import MaxRetryError, LocationParseError
from .packages.urllib3.exceptions import TimeoutError
from .packages.urllib3.exceptions import SSLError as _SSLError
from .packages.urllib3.exceptions import HTTPError as _HTTPError
from .packages.urllib3 import connectionpool, poolmanager
from .packages.urllib3.filepost import encode_multipart_formdata
from .defaults import SCHEMAS
from .exceptions import (
    ConnectionError, HTTPError, RequestException, Timeout, TooManyRedirects,
    URLRequired, SSLError, MissingSchema, InvalidSchema, InvalidURL)
from .utils import (
    get_encoding_from_headers, stream_untransfer, guess_filename, requote_uri,
    stream_decode_response_unicode, get_netrc_auth, get_environ_proxies,
    to_key_val_list, DEFAULT_CA_BUNDLE_PATH, parse_header_links, iter_slices,
    guess_json_utf)
from .compat import (
    cookielib, urlparse, urlunparse, urljoin, urlsplit, urlencode, str, bytes,
    StringIO, is_py2, chardet, json, builtin_str, urldefrag, basestring)

REDIRECT_STATI = (codes.moved, codes.found, codes.other, codes.temporary_moved)
CONTENT_CHUNK_SIZE = 10 * 1024

log = logging.getLogger(__name__)


class RequestMixin(object):

    @property
    def path_url(self):
        """Build the path URL to use."""

        url = []

        p = urlsplit(self.url)

        # Proxies use full URLs.
        # if p.scheme in self.proxies:
            # url_base, frag = urldefrag(self.url)
            # return url_base


        path = p.path
        if not path:
            path = '/'

        url.append(path)

        query = p.query
        if query:
            url.append('?')
            url.append(query)

        return ''.join(url)

    @staticmethod
    def _encode_params(data):
        """Encode parameters in a piece of data.

        Will successfully encode parameters when passed as a dict or a list of
        2-tuples. Order is retained if data is a list of 2-tuples but abritrary
        if parameters are supplied as a dict.
        """

        if isinstance(data, (str, bytes)):
            return data
        elif hasattr(data, 'read'):
            return data
        elif hasattr(data, '__iter__'):
            result = []
            for k, vs in to_key_val_list(data):
                if isinstance(vs, basestring) or not hasattr(vs, '__iter__'):
                    vs = [vs]
                for v in vs:
                    if v is not None:
                        result.append(
                            (k.encode('utf-8') if isinstance(k, str) else k,
                             v.encode('utf-8') if isinstance(v, str) else v))
            return urlencode(result, doseq=True)
        else:
            return data

    @staticmethod
    def _encode_files(files, data):
        """Build the body for a multipart/form-data request.

        Will successfully encode files when passed as a dict or a list of
        2-tuples. Order is retained if data is a list of 2-tuples but abritrary
        if parameters are supplied as a dict.

        """
        if (not files) or isinstance(data, str):
            return None

        new_fields = []
        fields = to_key_val_list(data)
        files = to_key_val_list(files)

        for field, val in fields:
            if isinstance(val, list):
                for v in val:
                    new_fields.append((field, builtin_str(v)))
            else:
                new_fields.append((field, builtin_str(val)))

        for (k, v) in files:
            # support for explicit filename
            if isinstance(v, (tuple, list)):
                fn, fp = v
            else:
                fn = guess_filename(v) or k
                fp = v
            if isinstance(fp, str):
                fp = StringIO(fp)
            if isinstance(fp, bytes):
                fp = BytesIO(fp)
            new_fields.append((k, (fn, fp.read())))

        body, content_type = encode_multipart_formdata(new_fields)

        return body, content_type


class Request(object):
    """A user-created :class:`Request <Request>` object."""
    def __init__(self,
        method=None,
        url=None,
        headers=None,
        files=None,
        data=dict(),
        params=dict(),
        auth=None,
        cookies=None,
        timeout=None,
        allow_redirects=False,
        proxies=None,
        hooks=None,
        prefetch=True,
        verify=None,
        cert=None):

        self.method = method
        self.url = url
        self.headers = headers
        self.files = files
        self.data = data
        self.params = params
        self.auth = auth
        self.cookies = cookies
        self.allow_redirects = allow_redirects
        self.proxies = proxies
        self.hooks = hooks

    def __repr__(self):
        return '<Request [%s]>' % (self.method)

    def prepare(self):
        """Constructs a PreparedRequest and returns it."""
        p = PreparedRequest()

        p.prepare_method(self.method)
        p.prepare_url(self.url, self.params)
        p.prepare_headers(self.headers)
        p.prepare_cookies(self.cookies)
        p.prepare_auth(self.auth)
        p.prepare_body(self.data, self.files)

        return p


class PreparedRequest(RequestMixin):
    """The :class:`PreparedRequest <PreparedRequest>` object."""

    def __init__(self):
        self.method = None
        self.url = None
        self.headers = None
        self.body = None
        self.params = None
        self.auth = None
        self.allow_redirects = None
        self.proxies = None
        self.hooks = None

    def __repr__(self):
        return '<PreparedRequest [%s]>' % (self.method)

    def prepare_method(self, method):
        """Prepares the given HTTP method."""
        try:
            method = unicode(method)
        except NameError:
            # We're on Python 3.
            method = str(method)

        self.method = method.upper()

    def prepare_url(self, url, params):
        """Prepares the given HTTP URL."""
        #: Accept objects that have string representations.
        try:
            url = unicode(url)
        except NameError:
            # We're on Python 3.
            url = str(url)
        except UnicodeDecodeError:
            pass

        # Support for unicode domain names and paths.
        scheme, netloc, path, params, query, fragment = urlparse(url)

        if not scheme:
            raise MissingSchema("Invalid URL %r: No schema supplied" % url)

        if not scheme in SCHEMAS:
            raise InvalidSchema("Invalid scheme %r" % scheme)

        try:
            netloc = netloc.encode('idna').decode('utf-8')
        except UnicodeError:
            raise InvalidURL('URL has an invalid label.')

        if not path:
            path = '/'

        if is_py2:
            if isinstance(scheme, str):
                scheme = scheme.encode('utf-8')
            if isinstance(netloc, str):
                netloc = netloc.encode('utf-8')
            if isinstance(path, str):
                path = path.encode('utf-8')
            if isinstance(params, str):
                params = params.encode('utf-8')
            if isinstance(query, str):
                query = query.encode('utf-8')
            if isinstance(fragment, str):
                fragment = fragment.encode('utf-8')

        enc_params = self._encode_params(params)
        if enc_params:
            if query:
                query = '%s&%s' % (query, enc_params)
            else:
                query = enc_params

        url = (urlunparse([scheme, netloc, path, params, query, fragment]))

        # if self.config.get('encode_uri', True):
            # url = requote_uri(url)
        # TODO: re-evaluate quote param (perhaps not, people can create this themselves now)
        url = requote_uri(url)

        self.url = url

    def prepare_headers(self, headers):
        """Prepares the given HTTP headers."""

        if headers:
            self.headers = CaseInsensitiveDict(headers)
        else:
            self.headers = CaseInsensitiveDict()

    def prepare_body(self, data, files):
        """Prepares the given HTTP body data."""
        # if a generator is provided, error out.

        # Nottin' on you.
        body = None
        content_type = None

        # Multi-part file uploads.
        if files:
            (body, content_type) = self._encode_files(files, data)
        else:
            if data:

                body = self._encode_params(data)
                if isinstance(data, str) or isinstance(data, builtin_str) or hasattr(data, 'read'):
                    content_type = None
                else:
                    content_type = 'application/x-www-form-urlencoded'

        self.headers['Content-Length'] = '0'
        if hasattr(body, 'seek') and hasattr(body, 'tell'):
            body.seek(0, 2)
            self.headers['Content-Length'] = str(body.tell())
            body.seek(0, 0)
        elif body is not None:
            self.headers['Content-Length'] = str(len(body))

        # Add content-type if it wasn't explicitly provided.
        if (content_type) and (not 'content-type' in self.headers):
            self.headers['Content-Type'] = content_type

        self.body = body

    def prepare_auth(self, auth):
        """Prepares the given HTTP auth data."""
        if auth:
            if isinstance(auth, tuple) and len(auth) == 2:
                # special-case basic HTTP auth
                auth = HTTPBasicAuth(*auth)

            # Allow auth to make its changes.
            r = auth(self)

            # Update self to reflect the auth changes.
            self.__dict__.update(r.__dict__)

    def prepare_cookies(self, cookies):
        """Prepares the given HTTP cookie data."""

        if isinstance(cookies, cookielib.CookieJar):
            cookies = cookies
        else:
            cookies = cookiejar_from_dict(cookies)

        if 'cookie' not in self.headers:
            cookie_header = get_cookie_header(cookies, self)
            if cookie_header is not None:
                self.headers['Cookie'] = cookie_header


class Response(object):
    """The core :class:`Response <Response>` object. All
    :class:`Request <Request>` objects contain a
    :class:`response <Response>` attribute, which is an instance
    of this class.
    """

    def __init__(self):
        super(Response, self).__init__()

        self._content = False
        self._content_consumed = False

        #: Integer Code of responded HTTP Status.
        self.status_code = None

        #: Case-insensitive Dictionary of Response Headers.
        #: For example, ``headers['content-encoding']`` will return the
        #: value of a ``'Content-Encoding'`` response header.
        self.headers = CaseInsensitiveDict()

        #: File-like object representation of response (for advanced usage).
        #: Requires that ``prefetch=False` on the request.
        # This requirement does not apply for use internally to Requests.
        self.raw = None

        #: Final URL location of Response.
        self.url = None

        #: Encoding to decode with when accessing r.text.
        self.encoding = None

        #: A list of :class:`Response <Response>` objects from
        #: the history of the Request. Any redirect responses will end
        #: up here. The list is sorted from the oldest to the most recent request.
        self.history = []

        #: The :class:`Request <Request>` that created the Response.
        # self.request = None

        self.reason = None

        #: A CookieJar of Cookies the server sent back.
        self.cookies = None

        #: Dictionary of configurations for this request.
        self.config = {}

    def __repr__(self):
        return '<Response [%s]>' % (self.status_code)

    def __bool__(self):
        """Returns true if :attr:`status_code` is 'OK'."""
        return self.ok

    def __nonzero__(self):
        """Returns true if :attr:`status_code` is 'OK'."""
        return self.ok

    @property
    def ok(self):
        try:
            self.raise_for_status()
        except RequestException:
            return False
        return True

    def iter_content(self, chunk_size=1, decode_unicode=False):
        """Iterates over the response data.  This avoids reading the content
        at once into memory for large responses.  The chunk size is the number
        of bytes it should read into memory.  This is not necessarily the
        length of each item returned as decoding can take place.
        """
        if self._content_consumed:
            # simulate reading small chunks of the content
            return iter_slices(self._content, chunk_size)

        def generate():
            while 1:
                chunk = self.raw.read(chunk_size)
                if not chunk:
                    break
                yield chunk
            self._content_consumed = True

        gen = stream_untransfer(generate(), self)

        if decode_unicode:
            gen = stream_decode_response_unicode(gen, self)

        return gen

    def iter_lines(self, chunk_size=10 * 1024, decode_unicode=None):
        """Iterates over the response data, one line at a time.  This
        avoids reading the content at once into memory for large
        responses.
        """

        pending = None

        for chunk in self.iter_content(
            chunk_size=chunk_size,
            decode_unicode=decode_unicode):

            if pending is not None:
                chunk = pending + chunk
            lines = chunk.splitlines()

            if lines and lines[-1] and chunk and lines[-1][-1] == chunk[-1]:
                pending = lines.pop()
            else:
                pending = None

            for line in lines:
                yield line

        if pending is not None:
            yield pending

    @property
    def content(self):
        """Content of the response, in bytes."""

        if self._content is False:
            # Read the contents.
            try:
                if self._content_consumed:
                    raise RuntimeError(
                        'The content for this response was already consumed')

                if self.status_code is 0:
                    self._content = None
                else:
                    self._content = bytes().join(self.iter_content(CONTENT_CHUNK_SIZE)) or bytes()

            except AttributeError:
                self._content = None

        self._content_consumed = True
        # don't need to release the connection; that's been handled by urllib3
        # since we exhausted the data.
        return self._content

    @property
    def text(self):
        """Content of the response, in unicode.

        if Response.encoding is None and chardet module is available, encoding
        will be guessed.
        """

        # Try charset from content-type
        content = None
        encoding = self.encoding

        if not self.content:
            return str('')

        # Fallback to auto-detected encoding.
        if self.encoding is None:
            if chardet is not None:
                encoding = chardet.detect(self.content)['encoding']

        # Decode unicode from given encoding.
        try:
            content = str(self.content, encoding, errors='replace')
        except (LookupError, TypeError):
            # A LookupError is raised if the encoding was not found which could
            # indicate a misspelling or similar mistake.
            #
            # A TypeError can be raised if encoding is None
            #
            # So we try blindly encoding.
            content = str(self.content, errors='replace')

        return content

    @property
    def json(self):
        """Returns the json-encoded content of a response, if any."""

        if not self.encoding and len(self.content) > 3:
            # No encoding set. JSON RFC 4627 section 3 states we should expect
            # UTF-8, -16 or -32. Detect which one to use; If the detection or
            # decoding fails, fall back to `self.text` (using chardet to make
            # a best guess).
            encoding = guess_json_utf(self.content)
            if encoding is not None:
                try:
                    return json.loads(self.content.decode(encoding))
                except (ValueError, UnicodeDecodeError):
                    pass
        try:
            return json.loads(self.text or self.content)
        except ValueError:
            return None

    def iter_json(self):
        for line in self.iter_lines():
            if line: # filter out keep-alive new lines
                yield json.loads(line)

    @property
    def links(self):
        """Returns the parsed header links of the response, if any."""

        header = self.headers['link']

        # l = MultiDict()
        l = {}

        if header:
            links = parse_header_links(header)

            for link in links:
                key = link.get('rel') or link.get('url')
                l[key] = link

        return l

    def raise_for_status(self):
        """Raises stored :class:`HTTPError` or :class:`URLError`, if one occurred."""

        http_error_msg = ''

        if 400 <= self.status_code < 500:
            http_error_msg = '%s Client Error: %s' % (self.status_code, self.reason)

        elif 500 <= self.status_code < 600:
            http_error_msg = '%s Server Error: %s' % (self.status_code, self.reason)

        if http_error_msg:
            http_error = HTTPError(http_error_msg)
            http_error.response = self
            raise http_error

    def close(self):
        return self.raw.release_conn()
