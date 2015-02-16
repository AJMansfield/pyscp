#!/usr/bin/env python3

###############################################################################
# Module Imports
###############################################################################

import arrow
import bs4
import cached_property
import collections
import concurrent.futures
import contextlib
import functools
import itertools
import logging
import re
import requests
import urllib.parse

import scp.orm

###############################################################################
# Global Constants And Variables
###############################################################################

log = logging.getLogger('scp.crawler')

###############################################################################
# Classes
###############################################################################


class WikidotConnector:

    """
    Provide a low-level interface to a Wikidot site.

    This class does not use any of the official Wikidot API, and instead
    relies on sending http post/get requests to internal Wikidot pages and
    parsing the returned data.
    """

    def __init__(self, site):
        parsed = urllib.parse.urlparse(site)
        netloc = parsed.netloc or parsed.path
        if '.' not in netloc:
            netloc += '.wikidot.com'
        self.site = urllib.parse.urlunparse(['http', netloc, '', '', '', ''])
        req = requests.Session()
        req.mount(site, requests.adapters.HTTPAdapter(max_retries=5))
        self.req = req

    ###########################################################################
    # Internal Methods
    ###########################################################################

    def _module(self, name, pageid, **kwargs):
        """
        Call a Wikidot module.

        This method is responsible for most of the class' functionality.
        Almost all other methods of the class are using _module in one way
        or another.
        """
        log.debug('_module call: {} ({}) {}'.format(name, pageid, kwargs))
        payload = {
            'page_id': pageid,
            'pageId': pageid,  # fuck wikidot
            'moduleName': name,
            # token7 can be any 6-digit number, as long as it's the same
            # in the payload and in the cookie
            'wikidot_token7': '123456'}
        payload.update(kwargs)
        cookies = {'wikidot_token7': '123456'}
        cookies.update({i.name: i.value for i in self.req.cookies})
        data = self.req.post(
            self.site + '/ajax-module-connector.php',
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded;'},
            cookies=cookies, timeout=30)
        if data.status_code != 200:
            log.warning(
                'Status code {} recieved from _module call: {} ({}) {}'
                .format(data.status_code, name, pageid, kwargs))
        return data.json()

    def _parse_forum_thread_page(self, page_html, thread_id):
        """Parse posts from an html string of a single forum page."""
        for tag in bs4.BeautifulSoup(page_html).select('div.post'):
            granpa = tag.parent.parent
            if 'class' in granpa.attrs and 'post-container' in granpa['class']:
                parent = granpa.select('div.post')[0]['id'].split('-')[1]
            else:
                parent = None
            yield {
                'post_id': tag['id'].split('-')[1],
                'thread_id': thread_id,
                'title': tag.select('div.title')[0].text.strip(),
                'content': tag.select('div.content')[0],
                'user': tag.select('span.printuser')[0].text,
                'time': (arrow.get(
                    tag.select('span.odate')[0]['class'][1].split('_')[1])
                    .format('YYYY-MM-DD HH:mm:ss')),
                'parent': parent}

    def _pager(self, baseurl):
        """
        Iterate over multi-page pages.

        Some Wikidot pages that seem to employ the paging mechanism
        actually don't. For example, discussion pages have a navigation
        bar that displays '<< previous' and 'next >>' buttons; however,
        discussion pages actually use separate calls to the
        ForumViewThreadPostsModule.
        """
        log.debug('Paging through {}'.format(baseurl))
        first_page = self.get_page_html(baseurl)
        yield first_page
        soup = bs4.BeautifulSoup(first_page)
        try:
            counter = soup.select('div.pager span.pager-no')[0].text
        except IndexError:
            return
        last_page_index = int(counter.split(' ')[-1])
        for index in range(2, last_page_index + 1):
            log.debug('Paging through {} ({}/{})'.format(
                baseurl, index, last_page_index))
            url = '{}/p/{}'.format(baseurl, index)
            yield self.get_page_html(url)

    ###########################################################################
    # Data Retrieval Methods
    ###########################################################################

    def get_page_html(self, url):
        """Download the html data of the page."""
        log.debug('Downloading page html: {}'.format(url))
        data = self.req.get(url, allow_redirects=False, timeout=30)
        if data.status_code == 200:
            return data.text
        else:
            msg = 'Page {} returned http status code {}'
            log.warning(msg.format(url, data.status_code))
            return None

    def get_page_history(self, pageid):
        """Download the revision history of the page."""
        if pageid is None:
            return None
        data = self._module(name='history/PageRevisionListModule',
                            pageid=pageid, page=1, perpage=1000000)['body']
        for i in bs4.BeautifulSoup(data).select('tr')[1:]:
            yield {
                'pageid': pageid,
                'number': int(i.select('td')[0].text.strip('.')),
                'user': i.select('td')[4].text,
                'time': (arrow.get(
                    i.select('td')[5].span['class'][1].split('_')[1])
                    .format('YYYY-MM-DD HH:mm:ss')),
                'comment': i.select('td')[6].text}

    def get_page_votes(self, pageid):
        """Download the vote data."""
        if pageid is None:
            return None
        data = self._module(name='pagerate/WhoRatedPageModule',
                            pageid=pageid)['body']
        for i in bs4.BeautifulSoup(data).select('span.printuser'):
            yield {'pageid': pageid, 'user': i.text, 'value': (
                1 if i.next_sibling.next_sibling.text.strip() == '+'
                else -1)}

    def get_page_source(self, pageid):
        """Download page source."""
        if pageid is None:
            return None
        data = self._module(
            name='viewsource/ViewSourceModule',
            pageid=pageid)['body']
        return bs4.BeautifulSoup(data).text[11:].strip()

    def get_forum_thread(self, thread_id):
        """Download and parse the contents of the forum thread."""
        if thread_id is None:
            return
        data = self._module(name='forum/ForumViewThreadPostsModule',
                            t=thread_id, pageid=None, pageNo=1)['body']
        try:
            pager = bs4.BeautifulSoup(data).select('span.pager-no')[0].text
            num_of_pages = int(pager.split(' of ')[1])
        except IndexError:
            num_of_pages = 1
        yield from self._parse_forum_thread_page(data, thread_id)
        for n in range(2, num_of_pages + 1):
            data = self._module(name='forum/ForumViewThreadPostsModule',
                                t=thread_id, pageid=None, pageNo=n)['body']
            yield from self._parse_forum_thread_page(data, thread_id)

    ###########################################################################
    # Site Structure Methods
    ###########################################################################

    def list_all_pages(self):
        """Yield urls of all the pages on the site."""
        for page in self._pager(self.site + '/system:list-all-pages'):
            for l in bs4.BeautifulSoup(page).select('div.list-pages-item a'):
                yield self.site + l['href']

    def list_categories(self):
        """Yield dicts describing all forum categories on the site."""
        baseurl = '{}/forum:start'.format(self.site)
        soup = bs4.BeautifulSoup(self.get_page_html(baseurl))
        for i in soup.select('td.name'):
            yield {
                'category_id': re.search(
                    r'/forum/c-([0-9]+)/',
                    i.select('div.title')[0].a['href']).group(1),
                'title': i.select('div.title')[0].text.strip(),
                'threads': int(i.parent.select('td.threads')[0].text),
                'description': i.select('div.description')[0].text.strip()}

    def list_threads(self, category_id):
        """Yield dicts describing all threads in a given category."""
        baseurl = '{}/forum/c-{}'.format(self.site, category_id)
        for page in self._pager(baseurl):
            for i in bs4.BeautifulSoup(page).select('td.name'):
                yield {
                    'thread_id': re.search(
                        r'/forum/t-([0-9]+)',
                        i.select('div.title')[0].a['href']).group(1),
                    'title': i.select('div.title')[0].text.strip(),
                    'description': i.select('div.description')[0].text.strip(),
                    'category_id': category_id}

    def list_tagged_pages(self, tag):
        """Return a list of all pages with a given tag."""
        url = '{}/system:page-tags/tag/{}'.format(self.site, tag)
        soup = bs4.BeautifulSoup(self.get_page_html(url))
        return [self.site + i['href'] for i in
                soup.select('div.pages-list-item a')]

    def recent_changes(self, num):
        """Return the last 'num' revisions on the site."""
        data = self._module(
            name='changes/SiteChangesListModule', pageid=None,
            options={'all': True}, page=1, perpage=num)['body']
        for tag in bs4.BeautifulSoup(data).select('div.changes-list-item'):
            revnum = tag.select('td.revision-no')[0].text.strip()
            time = tag.select('span.odate')[0]['class'][1].split('_')[1]
            comment = tag.select('div.comments')
            yield {
                'url': self.site + tag.select('td.title')[0].a['href'],
                'number': 0 if revnum == '(new)' else int(revnum[6:-1]),
                'user': tag.select('span.printuser')[0].text.strip(),
                'time': arrow.get(time).format('YYYY-MM-DD HH:mm:ss'),
                'comment': comment[0].text.strip() if comment else ''}

    ###########################################################################
    # Methods Requiring Authorization
    ###########################################################################

    def auth(self, username, password):
        """Login to wikidot with the given username/password pair."""
        data = {'login': username,
                'password': password,
                'action': 'Login2Action',
                'event': 'login'}
        self.req.post(
            'https://www.wikidot.com/default--flow/login__LoginPopupScreen',
            data=data)

    def edit_page(self, pageid, url, source, title, comments=None):
        """
        Overwrite the page with the new source and title.

        'pageid' and 'url' must belong to the same page.
        'comments' is the optional edit message that will be displayed in
        the page's revision history.
        """
        lock = self._module('edit/PageEditModule', pageid, mode='page')
        params = {
            'source': source,
            'comments': comments,
            'title': title,
            'lock_id': lock['lock_id'],
            'lock_secret': lock['lock_secret'],
            'revision_id': lock['page_revision_id'],
            'action': 'WikiPageAction',
            'event': 'savePage',
            'wiki_page': url.split('/')[-1]}
        self._module('Empty', pageid, **params)

    def post_in_thread(self, thread_id, source, title=None):
        """Make a new post in the given thread."""
        params = {
            'threadId': thread_id,
            # used for replying to other posts, not currently implemented.
            'parentId': None,
            'title': title,
            'source': source,
            'action': 'ForumAction',
            'event': 'savePost'}
        self._module('Empty', None, **params)

    def set_page_tags(self, pageid, tags):
        """Replace the tags of the page."""
        params = {
            'tags': ' '.join(tags),
            'action': 'WikiPageAction',
            'event': 'saveTags'}
        self._module('Empty', pageid, **params)


class Snapshot:

    """
    Create and manipulate a snapshot of a wikidot site.

    Snapshots are sqlite db files stored in the
    'database_directory' (see below). This class uses WikidotConnector to
    iterate over all the pages of a site, and save the html content,
    revision history, votes, and discussion page of each. Optionally,
    standalone forum threads can be saved too.

    In case of the scp-wiki, some additional information is saved:
    images for which their CC status has been confirmed, and info about
    overwriting page authorship.

    In general, this class will not save images hosted on the site that is
    being saved. Only the html content, discussions, and revision/vote
    metadata is saved.
    """

    database_directory = '/home/anqxyr/heap/_scp/'

    def __init__(self, dbname=None):
        if dbname is None:
            dbname = 'scp-wiki.{}.db'.format(arrow.now().format('YYYY-MM-DD'))
        orm.connect(self.database_directory + dbname)
        self.dbname = dbname
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=20)

    ###########################################################################
    # Internal Methods
    ###########################################################################

    def _scrape_images(self):
        #TODO: rewrite this to get images from the review pages
        url = "http://scpsandbox2.wikidot.com/ebook-image-whitelist"
        req = requests.Session()
        req.mount('http://', requests.adapters.HTTPAdapter(max_retries=5))
        soup = bs4.BeautifulSoup(req.get(url).text)
        data = []
        for i in soup.select("tr")[1:]:
            image_url = i.select("td")[0].text
            image_source = i.select("td")[1].text
            image_data = req.get(image_url).content
            data.append({
                "url": image_url,
                "source": image_source,
                "data": image_data})
        return data

    def _save_page(self, url):
        """Download the page and write it to the db."""
        log.info('Saving page: {}'.format(url))
        html = self.wiki.get_page_html(url)
        if html is None:
            return
        pageid, thread_id = _parse_html_for_ids(html)
        soup = bs4.BeautifulSoup(html)
        html = str(soup.select('#main-content')[0])  # cut off side-bar, etc.
        orm.Page.create(pageid=pageid, url=url, html=html, thread_id=thread_id)
        orm.Revision.insert_many(self.wiki.get_page_history(pageid))
        orm.Vote.insert_many(self.wiki.get_page_votes(pageid))
        orm.ForumPost.insert_many(self.wiki.get_forum_thread(thread_id))
        orm.Tag.insert_many({'tag': a.string, 'url': url} for a in
                            bs4.BeautifulSoup(html).select('div.page-tags a'))

    def _save_forums(self,):
        """Download and save standalone forum threads."""
        orm.ForumThread.create_table()
        orm.ForumCategory.create_table()
        categories = [
            i for i in self.wiki.list_categories()
            if i['title'] != 'Per page discussions']
        total = sum([i['threads'] for i in categories])
        index = itertools.count(1)
        futures = []
        _save = lambda x: (orm.ForumPost.insert_many(
            self.wiki.get_forum_thread(x['thread_id'])))
        for category in categories:
            orm.ForumCategory.create(**category)
            for thread in self.wiki.list_threads(category['category_id']):
                orm.ForumThread.create(**thread)
                log.info(
                    'Saving forum thread #{}/{}: {}'
                    .format(next(index), total, thread['title']))
                futures.append(self.pool.submit(_save, thread))
        return futures

    ###########################################################################
    # Page Interface
    ###########################################################################

    def get_page_html(self, url):
        try:
            return orm.Page.get(orm.Page.url == url).html
        except orm.Page.DoesNotExist:
            return None

    def get_pageid(self, url):
        try:
            return orm.Page.get(orm.Page.url == url).pageid
        except orm.Page.DoesNotExist:
            return None

    def get_thread_id(self, url):
        return orm.Page.get(orm.Page.url == url).thread_id

    def get_page_history(self, pageid):
        query = (orm.Revision.select()
                 .where(orm.Revision.pageid == pageid)
                 .order_by(orm.Revision.number))
        history = []
        for i in query:
            history.append({
                'pageid': pageid,
                'number': i.number,
                'user': i.user,
                'time': i.time,
                'comment': i.comment})
        return history

    def get_page_votes(self, pageid):
        for i in orm.Vote.select().where(orm.Vote.pageid == pageid):
            yield {a: getattr(i, a) for a in ('pageid', 'user', 'value')}

    def get_page_tags(self, url):
        query = orm.Tag.select().where(orm.Tag.url == url)
        tags = []
        for tag in query:
            tags.append(tag.tag)
        return tags

    def get_forum_thread(self, thread_id):
        query = (orm.ForumPost.select()
                 .where(orm.ForumPost.thread_id == thread_id)
                 .order_by(orm.ForumPost.post_id))
        posts = []
        for i in query:
            posts.append({
                'thread_id': thread_id,
                'post_id': i.post_id,
                'title': i.title,
                'content': i.content,
                'user': i.user,
                'time': i.time,
                'parent': i.parent})
        return posts

    ###########################################################################
    # Public Methods
    ###########################################################################

    def take(self, site='http://www.scp-wiki.net', include_forums=False):
        self.wiki = WikidotConnector(site)
        time_start = arrow.now()
        orm.purge()
        for i in [orm.Page, orm.Revision, orm.Vote, orm.ForumPost, orm.Tag]:
            i.create_table()
        ftrs = [self.pool.submit(self._save_page, i)
                for i in self.wiki.list_all_pages()]
        concurrent.futures.wait(ftrs)
        if include_forums:
            ftrs = self._save_forums()
            concurrent.futures.wait(ftrs)
        if site == 'http://www.scp-wiki.net':
            orm.Image.create_table()
            log.info('Downloading image metadata.')
            orm.Image.insert_many(self._scrape_images())
            orm.Author.create_table()
            log.info('Downloading author metadata.')
            orm.Author.insert_many(_get_rewrite_list())
        orm.queue.join()
        time_taken = (arrow.now() - time_start)
        hours, remainder = divmod(time_taken.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        msg = 'Snapshot succesfully taken. [{:02d}:{:02d}:{:02d}]'
        msg = msg.format(hours, minutes, seconds)
        log.info(msg)

    def list_tagged_pages(self, tag):
        """Retrieve list of pages with the tag from the database"""
        for i in orm.Tag.select().where(orm.Tag.tag == tag):
            yield i.url

    def get_rewrite_list(self):
        for au in orm.Author.select():
            yield {i: getattr(au, i) for i in ('url', 'author', 'override')}

    def get_image_metadata(self, url):
        try:
            img = orm.Image.get(orm.Image.url == url)
            return {'url': img.url, 'source': img.source, 'data': img.data}
        except orm.Image.DoesNotExist:
            return None

    def list_all_pages(self):
        count = orm.Page.select().count()
        for n in range(1, count // 200 + 2):
            query = orm.Page.select(
                orm.Page.url).order_by(
                orm.Page.url).paginate(n, 200)
            for i in query:
                yield i.url


class Page:

    """ """

    _connector = None

    ###########################################################################
    # Constructors
    ###########################################################################

    def __init__(self, url=None):
        if url is not None:
            parsed = urllib.parse.urlparse(url)
            if not parsed.netloc:
                url = urllib.parse.urljoin(self._connector.site, url)
        self.url = url

    ###########################################################################
    # Class Methods
    ###########################################################################

    @classmethod
    @contextlib.contextmanager
    def load_from(cls, connector):
        previous_connector = cls._connector
        cls._connector = connector
        yield connector
        if hasattr(previous_connector, 'dbname'):
            # Snapshots need to explicitely reconnect to their db
            orm.connect(previous_connector.dbname)
        cls._connector = previous_connector

    ###########################################################################
    # Internal Methods
    ###########################################################################

    def _get_children_if_skip(self):
        children = []
        for url in self.links:
            p = self.__class__(url)
            if 'supplement' in p.tags or 'splash' in p.tags:
                children.append(p.url)
        return children

    def _get_children_if_hub(self):
        maybe_children = []
        confirmed_children = []
        for url in self.links:
            p = self.__class__(url)
            if set(p.tags) & {'tale', 'goi-format', 'goi2014'}:
                maybe_children.append(p.url)
            if self.url in p.links:
                confirmed_children.append(p.url)
            elif p.html:
                crumb = bs4.BeautifulSoup(p.html).select('#breadcrumbs a')
                if crumb:
                    parent = crumb[-1]
                    parent = 'http://www.scp-wiki.net{}'.format(parent['href'])
                    if self.url == parent:
                        confirmed_children.append(p.url)
        if confirmed_children:
            return confirmed_children
        else:
            return maybe_children

    @classmethod
    @functools.lru_cache()
    def _title_index(cls):
        log.debug('Constructing title index.')
        index_pages = ['scp-series', 'scp-series-2', 'scp-series-3']
        index = {}
        for url in index_pages:
            soup = bs4.BeautifulSoup(cls(url).html)
            items = [i for i in soup.select('ul > li')
                     if re.search('[SCP]+-[0-9]+', i.text)]
            for i in items:
                url = cls._connector.site + i.a['href']
                try:
                    skip, title = i.text.split(' - ', maxsplit=1)
                except ValueError:
                    skip, title = i.text.split(', ', maxsplit=1)
                if url not in cls._connector.list_tagged_pages('splash'):
                    index[url] = title
                else:
                    true_url = '{}/{}'.format(
                        cls._connector.site, skip.lower())
                    index[true_url] = title
        return index

    @classmethod
    @functools.lru_cache()
    def _rewrite_list(cls):
        try:
            rewrite_list = cls.get_rewrite_list()
        except AttributeError:
            rewrite_list = _get_rewrite_list()
        return {i['url']: (i['author'], i['override']) for i in rewrite_list}

    ###########################################################################
    # Internal Properties
    ###########################################################################

    @cached_property.cached_property
    def _pageid(self):
        if hasattr(self._connector, 'get_pageid'):
            return self._connector.get_pageid(self.url)
        pageid, thread_id = _parse_html_for_ids(self.html)
        self._thread_id = thread_id
        return pageid

    @cached_property.cached_property
    def _thread_id(self):
        if hasattr(self._connector, 'get_thread_id'):
            return self._connector.get_thread_id(self.url)
        pageid, thread_id = _parse_html_for_ids(self.html)
        self._pageid = pageid
        return thread_id

    @cached_property.cached_property
    def _wikidot_title(self):
        '''
        Page title as used by wikidot. Should only be used by the self.title
        property or when editing the page. In all other cases, use self.title.
        '''
        tag = bs4.BeautifulSoup(self.html).select('#page-title')
        return tag[0].text.strip() if tag else ''

    ###########################################################################
    # Public Properties
    ###########################################################################

    @cached_property.cached_property
    def html(self):
        return self._connector.get_page_html(self.url)

    @cached_property.cached_property
    def text(self):
        return bs4.BeautifulSoup(self.html).select('#page-content')[0].text

    @cached_property.cached_property
    def wordcount(self):
        return len(re.findall(r"[\w'█_-]+", self.text))

    @cached_property.cached_property
    def images(self):
        return [i['src'] for i in bs4.BeautifulSoup(self.html).select('img')]

    @cached_property.cached_property
    def title(self):
        if 'scp' in self.tags and re.search('[scp]+-[0-9]+$', self.url):
            title = '{}: {}'.format(
                self._wikidot_title,
                self._title_index()[self.url])
            return title
        return self._wikidot_title

    @cached_property.cached_property
    def history(self):
        data = self._connector.get_page_history(self._pageid)
        rev = collections.namedtuple('Revision', 'number user time comment')
        history = []
        for i in data:
            history.append(rev(
                i['number'],
                i['user'],
                i['time'],
                i['comment']))
        return history

    @cached_property.cached_property
    def created(self):
        return self.history[0].time

    @cached_property.cached_property
    def authors(self):
        if self.url is None:
            return None
        Author = collections.namedtuple('Author', 'user status')
        author_original = Author(self.history[0].user, 'original')
        if self.url not in self._rewrite_list():
            return [author_original]
        author, override = self._rewrite_list()[self.url]
        new_author = Author(author, 'override' if override else 'rewrite')
        return [author_original, new_author]

    @cached_property.cached_property
    def author(self):
        if len(self.authors) == 0:
            return None
        if len(self.authors) == 1:
            return self.authors[0].user
        else:
            for i in self.authors:
                if i.status == 'override':
                    return i.user
                if i.status == 'original':
                    original_author = i.user
            return original_author

    @cached_property.cached_property
    def votes(self):
        data = self._connector.get_page_votes(self._pageid)
        vote = collections.namedtuple('Vote', 'user value')
        votes = []
        for i in data:
            votes.append(vote(i['user'], i['value']))
        return votes

    @cached_property.cached_property
    def rating(self):
        if not self.votes:
            return None
        return sum(vote.value for vote in self.votes
                   if vote.user != '(account deleted)')

    @cached_property.cached_property
    def tags(self):
        try:
            return self._connector.get_page_tags(self.url)
        except AttributeError:
            return [a.string for a in
                    bs4.BeautifulSoup(self.html).select('div.page-tags a')]

    @cached_property.cached_property
    def comments(self):
        attributes = 'post_id parent title user time content'
        ForumPost = collections.namedtuple('ForumPost', attributes)
        return [ForumPost(*[i[k] for k in attributes.split(' ')])
                for i in self._connector.get_forum_thread(self._thread_id)]

    @cached_property.cached_property
    def links(self):
        if self.html is None:
            return []
        links = set()
        for a in bs4.BeautifulSoup(self.html).select('#page-content a'):
            if (('href' not in a.attrs) or
                (a['href'][0] != '/') or
                    (a['href'][-4:] in ['.png', '.jpg', '.gif'])):
                continue
            url = 'http://www.scp-wiki.net{}'.format(a['href'])
            url = url.rstrip("|")
            links.add(url)
        return list(links)

    @cached_property.cached_property
    def children(self):
        if 'scp' in self.tags or 'splash' in self.tags:
            return self._get_children_if_skip()
        if 'hub' in self.tags and (set(self.tags) & {'tale', 'goi2014'}):
            return self._get_children_if_hub()
        return []

###############################################################################
# Module-level Functions
###############################################################################


def _get_rewrite_list():
    """Download author override metadata from 05command."""
    soup = bs4.BeautifulSoup(
        WikidotConnector('http://05command.wikidot.com')
        .get_page_html('http://05command.wikidot.com/alexandra-rewrite'))
    for i in soup.select('tr')[1:]:
        yield {
            'url': 'http://www.scp-wiki.net/{}'.format(i.select('td')[0].text),
            'author': i.select('td')[1].text.split(':override:')[-1],
            'override': i.select('td')[1].text.startswith(':override:')}


def _parse_html_for_ids(html):
    pageid = re.search('pageId = ([^;]*);', html)
    pageid = pageid.group(1) if pageid is not None else None
    soup = bs4.BeautifulSoup(html)
    try:
        thread_id = re.search(r'/forum/t-([0-9]+)/', soup.select(
            '#discuss-button')[0]['href']).group(1)
    except (IndexError, AttributeError):
        thread_id = None
    return pageid, thread_id


def configure_logging(log):
    log.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    logging.getLogger('scp').addHandler(console)
    file = logging.FileHandler('logfile.txt', mode='w', delay=True)
    file.setLevel(logging.WARNING)
    file.setFormatter(logging.Formatter(
        '%(asctime)s %(name)-12s %(levelname)-8s %(message)s'))
    logging.getLogger('scp').addHandler(file)


def main():
    Snapshot().take()
    pass


if __name__ == "__main__":
    configure_logging(log)
    main()