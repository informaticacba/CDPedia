#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright 2010-2020 CDPedistas (see AUTHORS.txt)
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# For further info, check  http://code.google.com/p/cdpedia/

"""Download the whole wikipedia."""

from __future__ import with_statement, unicode_literals

import StringIO
import codecs
import collections
import datetime
import gzip
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib
import urllib2

import concurrent.futures

# import stuff from project's trunk
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from src.armado import to3dirs  # NOQA import after fixing path

# log all bad stuff
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler("scraper.log")
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s  %(message)s")
handler.setFormatter(formatter)

WIKI = 'http://%(lang)s.wikipedia.org/'

HISTORY_BASE = (
    'http://%(lang)s.wikipedia.org/w/api.php?action=query&prop=revisions'
    '&format=json&rvprop=ids|timestamp|user|userid'
    '&rvlimit=%(limit)d&titles=%(title)s'
)
REVISION_URL = (
    'http://%(lang)s.wikipedia.org/w/index.php?'
    'title=%(title)s&oldid=%(revno)s'
)

REQUEST_HEADERS = {
    'Accept-encoding': 'gzip',
    'User-Agent': "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:77.0) Gecko/20100101 Firefox/77.0",
}

DataURLs = collections.namedtuple("DataURLs", "url temp_dir disk_name, basename")


class URLAlizer(object):
    def __init__(self, listado_nombres, dest_dir, language, test_limit):
        self.language = language
        self.dest_dir = dest_dir
        self.temp_dir = dest_dir + ".tmp"
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
        self.fh = codecs.open(listado_nombres, 'r', encoding='utf-8')
        self.test_limit = test_limit

    def next(self):
        while True:
            if self.test_limit is not None:
                self.test_limit -= 1
                if self.test_limit <= 0:
                    raise StopIteration
            line = self.fh.readline().strip()
            if line == "":
                raise StopIteration
            if line == "page_title":
                continue
            basename = line.strip()
            three_dirs, filename = to3dirs.get_path_file(basename)
            path = os.path.join(self.dest_dir, three_dirs)
            disk_name = os.path.join(path, filename)
            if not os.path.exists(disk_name.encode('utf-8')):
                if not os.path.exists(path.encode('utf-8')):
                    os.makedirs(path.encode('utf-8'))

                quoted_url = urllib.quote(basename.encode('utf-8'))
                # Skip wikipedia automatic redirect
                wiki = WIKI % dict(lang=self.language)
                url = wiki + "w/index.php?title=%s&redirect=no" % (quoted_url,)
                data = DataURLs(url=url, temp_dir=self.temp_dir,
                                disk_name=disk_name, basename=basename)
                return data

    def __iter__(self):
        return self


def fetch_html(url):
    """Fetch an url following redirects."""
    retries = 3
    while True:
        try:
            req = urllib2.Request(url.encode('utf-8'), headers=REQUEST_HEADERS)
            resp = urllib2.urlopen(req, timeout=60)
            compressedstream = StringIO.StringIO(resp.read())
            gzipper = gzip.GzipFile(fileobj=compressedstream)
            html = gzipper.read().decode('utf-8')

            return html
        except Exception as err:
            if isinstance(err, urllib2.HTTPError) and err.code == 404:
                raise
            retries -= 1
            if not retries:
                raise


class PageHaveNoRevisions(Exception):
    pass


class WikipediaArticleHistoryItem(object):
    def __init__(self, user_registered, page_rev_id, date):
        self.user_registered = user_registered
        self.page_rev_id = page_rev_id
        self.date = date

    @classmethod
    def FromJSON(cls, jsonitem):
        user_registered = jsonitem.get('userid', 0) != 0
        page_rev_id = str(jsonitem['revid'])
        tstamp = jsonitem['timestamp']
        date = datetime.datetime.strptime(tstamp, "%Y-%m-%dT%H:%M:%SZ")
        return cls(user_registered, page_rev_id, date)

    def __str__(self):
        return '<rev: regist %s id %r %r>' % (self.user_registered,
                                              self.page_rev_id, self.date)


class WikipediaArticle(object):
    """Represent a wikipedia page.

    It should know how to retrive the asociated history page and any revision.
    """
    HISTORY_CLASS = WikipediaArticleHistoryItem

    def __init__(self, language, url, basename):
        self.language = language
        self.url = url
        self.basename = basename
        self.quoted_basename = urllib.quote(basename.encode('utf-8')).replace(' ', '_')
        self._history = None
        self.history_size = 6

    def __str__(self):
        return '<wp: %s>' % (self.basename.encode('utf-8'),)

    @property
    def history_url(self):
        url = HISTORY_BASE % dict(lang=self.language, limit=self.history_size,
                                  title=self.quoted_basename)
        return url

    def get_revision_url(self, revision=None):
        """
        Return the revision url when revision is provided, elsewhere the basic
        url for the page
        """
        if revision is None:
            return self.url
        url = REVISION_URL % dict(lang=self.language, title=self.quoted_basename, revno=revision)
        return url

    def get_history(self, size=6):
        if self._history is None or size != self.history_size:
            self.history_size = size
            self._history = fetch_html(self.history_url)
        return self._history

    def iter_history_json(self, json_rev_history):
        pages = json_rev_history['query']['pages']
        assert len(pages) == 1
        pageid = pages.keys()[0]
        if pageid == '-1':
            # page deleted / moved / whatever but not now..
            raise PageHaveNoRevisions(self)

        revisions = pages[pageid].get("revisions")
        if not revisions:
            # None, or there but empty
            # page deleted / moved / whatever but not now..
            raise PageHaveNoRevisions(self)

        for idx, item in enumerate(revisions):
            yield idx, self.HISTORY_CLASS.FromJSON(item)

    def search_valid_version(self, acceptance_days=7, _show_debug_info=False):
        """Search for a "good-enough" version of the page wanted.

        Where good-enough means:

         * Page version is commited by a registered user (being it
           human or bot).

         * Page version is commited by an unregistered user and stayed
           alive longer than 'acceptance_days'.

        Return None if no version page was found.

        For more info, check issue #124 at:
            http://code.google.com/p/cdpedia/issues/detail?id=124
        """
        self.acceptance_delta = datetime.timedelta(acceptance_days)
        idx, hist = self.iterate_history()
        if idx != 0:
            logger.info("Possible vandalism (idx=%d) in %r", idx, self.basename)
        return self.get_revision_url(hist.page_rev_id)

    def iterate_history(self):
        prev_date = datetime.datetime.now()

        for history_size in [6, 100]:
            history = self.get_history(size=history_size)
            json_rev_history = json.loads(history)

            for idx, hist in self.iter_history_json(json_rev_history):
                if self.validate_revision(hist, prev_date):
                    return (idx, hist)
                prev_date = hist.date

        return (idx, hist)

    def validate_revision(self, hist_item, prev_date):
        # if the user is registered, it's enough for us! (even if it's a bot)
        if hist_item.user_registered:
            return True
        # if it's not registered, check for how long this version lasted
        if hist_item.date + self.acceptance_delta < prev_date:
            return True
        return False


regex = (
    r'(<h1 id="firstHeading" class="firstHeading" '
    r'lang=".+">.+</h1>)(.+)\s*<div class="printfooter">')
capture = re.compile(regex, re.MULTILINE | re.DOTALL).search


def extract_content(html, url):
    found = capture(html)
    if not found:
        # unknown html format
        raise ValueError("HTML file from %s has an unknown format" % url)
    return "\n".join(found.groups())


def get_html(url, basename):
    try:
        html = fetch_html(url)
    except urllib2.HTTPError as err:
        if err.code == 404:
            logger.info("HTML not found (404): %s", basename)
        else:
            logger.info("Try again (HTTP error %s): %s", err.code, basename)
        return
    except Exception as err:
        logger.info("Try again (Exception while fetching: %r): %s", err, basename)
        return

    if not html:
        logger.info("Got an empty file: %r", url)

    # ok, downloaded the html, let's check that it complies with some rules
    if "</html>" not in html:
        # we surely didn't download it all
        logger.info("Try again (unfinished download): %s", basename)
        return

    try:
        html = extract_content(html, url)
    except ValueError as e:
        logger.info("Try again (Exception while extracting content: %r): %s", e, basename)
        return

    return html


def obtener_link_200_siguientes(html):
    links = re.findall('<a href="([^"]+)[^>]+>200 siguientes</a>', html)
    if links == []:
        return
    return '%s%s' % (WIKI[:-1], links[0])


def reemplazar_links_paginado(html, n):
    ''' Reemplaza lar urls anteriores y siguientes

        En el caso de la primera no encontrará el link 'anterior', no hay problema
        con llamar esta función
    '''

    def reemplazo(m):
        pre, link, post = m.groups()
        idx = '"' if (n == 2 and delta == -1) else '_%d"' % (n + delta)
        return '<a href="/wiki/' + link.replace('_', ' ') + idx + post

    # Reemplazo el link 'siguiente'
    delta = 1
    html = re.sub(r'(<a href="/w/index.php\?title=)(?P<link>[^&]+)[^>]+(>200 siguientes</a>)',
                  reemplazo, html)

    # Reemplazo el link 'anterior'
    delta = -1
    return re.sub(r'(<a href="/w/index.php\?title=)(?P<link>[^&]+)[^>]+(>200 previas</a>)',
                  reemplazo, html)


def get_temp_file(temp_dir):
    return tempfile.NamedTemporaryFile(suffix='.html', prefix='scrap-', dir=temp_dir, delete=False)


def save_htmls(data_url):
    """Save the article to a temporary file.

    If it is a category, process pagination and save all pages.
    """
    html = get_html(str(data_url.url), data_url.basename)
    # XXX Facundo (2020-06-15): This model of return None if had a problem sucks, need to
    # dig deeper in get_html and start raising an exception
    if html is None:
        return

    temp_file = get_temp_file(data_url.temp_dir)

    if "Categoría" not in data_url.basename:
        # normal case, not Categories or any paginated stuff
        temp_file.write(html.encode('utf-8'))
        temp_file.close()
        return [(temp_file, data_url.disk_name)]

    temporales = []
    # cat!
    n = 1

    while True:

        if n == 1:
            temporales.append((temp_file, data_url.disk_name))
        else:
            temporales.append((temp_file, data_url.disk_name + '_%d' % n))

        # encontrar el link tomando url
        prox_url = obtener_link_200_siguientes(html)

        html = reemplazar_links_paginado(html, n)
        temp_file.write(html.encode('utf-8'))
        temp_file.close()

        if not prox_url:
            return temporales

        html = get_html(prox_url.replace('&amp;', '&'), data_url.basename)
        if html is None:
            return

        temp_file = get_temp_file(data_url.temp_dir)
        n += 1


def fetch(data_url, language):
    """Fetch a wikipedia page (that can be paginated)."""
    page = WikipediaArticle(language, data_url.url, data_url.basename)
    try:
        url = page.search_valid_version()
    except PageHaveNoRevisions:
        logger.error("Version not found: %s", data_url.basename)
        return False
    except Exception:
        logger.exception("Error while getting valid version for %r", data_url.url)
        return False
    data_url = data_url._replace(url=url)

    # save the htmls with the (maybe changed) url and all the data
    temporales = save_htmls(data_url)
    if temporales is None:
        logger.error("Error saving htmls.")
        return False

    # transform temp data into final files
    for temp_file, disk_name in temporales:
        try:
            os.rename(temp_file.name, disk_name.encode("utf-8"))
        except OSError as e:
            logger.exception(
                "Try again (Error creating file %r: %r): %s", disk_name, e, data_url.basename)
            return False

    # return True when it was OK!
    return True


class StatusBoard(object):

    def __init__(self, language):
        self.total = 0
        self.bien = 0
        self.mal = 0
        self.tiempo_inicial = time.time()
        self.language = language

    def process(self, data_url):
        try:
            ok = fetch(data_url, self.language)
        except Exception:
            self.total += 1
            self.mal += 1
            raise
        else:
            self.total += 1
            if ok:
                self.bien += 1
            else:
                self.mal += 1
        finally:
            velocidad = self.total / (time.time() - self.tiempo_inicial)
            sys.stdout.write("\rTOTAL=%d  BIEN=%d  MAL=%d  vel=%.2f art/s" % (
                self.total, self.bien, self.mal, velocidad))
            sys.stdout.flush()


def main(nombres, language, dest_dir, namespaces_path, test_limit=None, pool_size=20):
    """Main entry point."""
    # fix namespaces in to3dirs module so we can use it in this stage
    to3dirs.namespaces = to3dirs.Namespaces(namespaces_path)

    test_limit = int(test_limit) if test_limit else None
    data_urls = URLAlizer(nombres, dest_dir, language, test_limit)

    board = StatusBoard(language)
    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as executor:
        # need to cosume the generator, but don't care about the results (board.process always
        # return None
        list(executor.map(board.process, data_urls))


USAGE = """
Usar: scraper.py <NOMBRES_ARTICULOS> <LANGUAGE> <DEST_DIR> [CONCURRENT]"
  Descarga la wikipedia escrapeándola.

  NOMBRES_ARTICULOS es un listado de nombres de artículos. Debe ser descargado
  y descomprimido de:
  http://download.wikipedia.org/eswiki/latest/eswiki-latest-all-titles-in-ns0.gz

  DEST_DIR es el directorio de destino, donde se guardan los artículos. Puede
  ocupar unos 40GB o más.

  CONCURRENT es la cantidad de corrutinas que realizan la descarga. Se puede
  tunear para incrementar velocidad de artículos por segundo. Depende mayormente
  de la conexión: latencia, ancho de banda, etc. El default es 20.

  Los nombres de los artículos que no pudieron descargarse correctamente se
  guardan en probar_de_nuevo.txt.

"""

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(USAGE)
        sys.exit(1)

    main(*sys.argv[1:])
