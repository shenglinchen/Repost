"""
This module contains helper classes and methods to assist with the collection of content
to be posted to Mastodon and/or Twitter
"""

import configparser
import hashlib
import os
import re
import sys
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import praw
import prawcore.exceptions
import requests
from PIL import Image
from bs4 import BeautifulSoup
from gfycat.client import GfycatClient
from gfycat.error import GfycatClientError
from imgurpython import ImgurClient
from imgurpython.helpers.error import ImgurClientError

FATAL_TOOTBOT_ERROR = 'Tootbot cannot continue, now shutting down'


# Function for downloading images from a URL to media folder
def save_file(img_url, file_path, logger):
    """
    Utility method to save a file located at img_url to a file located at filepath

        Arguments:
            img_url (string): url of imgur image to download
            file_path (string): directory and filename where to save the downloaded image to
            logger (logger): logger to use for logging messages

        Returns:
            file_path (string): path to downloaded image or None if no image was downloaded
    """
    resp = requests.get(img_url, stream=True)
    if resp.status_code == 200:
        with open(file_path, 'wb') as image_file:
            for chunk in resp:
                image_file.write(chunk)
        # Return the path of the image, which is always the same since we
        # just overwrite images
        image_file.close()
        return file_path

    logger.error('File failed to download. Status code: %s' % resp.status_code)
    return None


class RedditHelper:
    """
    RedditHelper provides methods to collect data / content from reddit to then post on
    Mastodon/Twitter
    """

    # Check if reddit access details in 'reddit.secret' file has already been set-up and load it.
    # otherwise guide user through setting it up.
    def __init__(self, logger, user_agent='Tootbot', config_file='reddit.secret'):
        self.logger = logger
        self.user_agent = user_agent
        self.allow_nsfw = False
        self.allow_self = False
        self.allow_spoilers = False

        reddit_config = configparser.ConfigParser()
        if not os.path.exists(config_file):
            logger.warning('Reddit API keys not found. (See wiki if you need help).')
            # Whitespaces are stripped from input: https://stackoverflow.com/a/3739939
            reddit_agent = ''.join(input("[ .. ] Enter Reddit agent: ").split())
            reddit_client_secret = ''.join(input("[ .. ] Enter Reddit client secret: ").split())
            reddit_username = ''.join(input("[ .. ] Enter Reddit username: ").split())
            reddit_password = ''.join(input("[ .. ] Enter Reddit password: ").split())

            # Make sure authentication is working
            try:
                reddit_client = praw.Reddit(user_agent=self.user_agent, client_id=reddit_agent,
                                            client_secret=reddit_client_secret, username=reddit_username,
                                            password=reddit_password)
                reddit_client.subreddit('announcements')
                # It worked, so save the keys to a file
                reddit_config['Reddit'] = {'Agent': reddit_agent,
                                           'ClientSecret': reddit_client_secret,
                                            'UserName': reddit_username,
                                            'password': reddit_password
                                           }
                with open(config_file, 'w') as new_reddit_secrets_file:
                    reddit_config.write(new_reddit_secrets_file)
                new_reddit_secrets_file.close()
            except prawcore.exceptions.ResponseException as reddit_exception:
                logger.error('Error while logging into Reddit: %s', reddit_exception)
                logger.error(FATAL_TOOTBOT_ERROR)
                sys.exit(1)
        else:
            # Read API keys from secret file
            reddit_config.read(config_file)

        self.reddit_connection = praw.Reddit(user_agent=self.user_agent,
                                             client_id=reddit_config['Reddit']['Agent'],
                                             client_secret=reddit_config['Reddit']['ClientSecret'],
                                             username=reddit_config["Reddit"]['UserName'],
                                             password=reddit_config["Reddit"]['password']),

    def get_reddit_posts(self, subreddit, limit=10):
        """
        get_reddit_posts reads up to "limit" number of posts from a given subreddit and returns
        them as a dict of

        Arguments:
            subreddit (string): name of subreddit (without leading "r/") to collect posts from
            limit (int): maximum number of posts to return (default 10)

        Returns:
            posts (dict): of posts to subreddit. each entry has a key of subreddit post-id
        """
        posts = {}
        self.logger.info('Getting posts from Subreddit: "%s"' % subreddit)
        subreddit_info = self.reddit_connection.subreddit(subreddit)
        try:
            for submission in subreddit_info.hot(limit=limit):

                if submission.over_18 and self.allow_nsfw is False:
                    # Skip over NSFW posts if they are disabled in the config file
                    self.logger.info('Skipping %s because it is marked as NSFW' % submission.id)
                    continue

                if submission.is_self and self.allow_self is False:
                    # Skip over NSFW posts if they are disabled in the config file
                    self.logger.info('Skipping %s because it is a self post' % submission.id)
                    continue

                if submission.spoiler and self.allow_spoilers is False:
                    # Skip over posts marked as spoilers if they are disabled in
                    # the config file
                    self.logger.info('Skipping %s because it is marked as a spoiler', submission.id)
                    continue

                if submission.stickied:
                    self.logger.info('Skipping %s because it is stickied' % submission.id)
                    continue

                # Create dict
                posts[submission.id] = submission
        except prawcore.exceptions.ResponseException as reddit_exception:
            self.logger.warn('Encountered and error getting reddit posts: $%', reddit_exception)

        return posts


class LinkedMediaHelper:
    """
    ImgurHelper provides methods to collect data / content from Imgur and Gfycat
    """

    def __init__(self, save_dir, logger,
                 imgur_secrets='imgur.secret',
                 gfycat_secrets='gfycat.secret',
                 ):
        self.logger = logger
        self.save_dir = save_dir

        try:
            imgur_config = self._get_imgur_secrets(imgur_secrets)
            self.imgur_client = ImgurClient(imgur_config['Imgur']['ClientID'],
                                            imgur_config['Imgur']['ClientSecret'],
                                            )

            gfycat_config = self._get_gfycat_secrets(gfycat_secrets)
            self.gfycat_client = GfycatClient(gfycat_config['Gfycat']['ClientID'],
                                              gfycat_config['Gfycat']['ClientSecret'],
                                              )

        except ImgurClientError as imgur_error:
            self.logger.error('Error on creating ImgurClient: %s', imgur_error)
            self.logger.error(FATAL_TOOTBOT_ERROR)
            sys.exit(1)
        except GfycatClientError as gfycat_error:
            self.logger.error('Error on creating GfycatClient: %s', gfycat_error)
            self.logger.error(FATAL_TOOTBOT_ERROR)
            sys.exit(1)

    def _get_gfycat_secrets(self, gfycat_secrets):
        """
        _get_gfycat_secrets checks if the Gfycat api secrets file exists.
        - If the file exists, this methods reads the the files and returns the secrets in as a dict.
        - If the file doesn't exist it asks the user over stdin to supply these values and then
          saves them into the gfycat_secrets file

        Arguments:
            gfycat_secrets (string): file name of secrets file for API credentials

        Returns:
            imgur_config (dict): Dictionary containing the client id and client secret needed to
            login to Gfycat
        """

        if not os.path.exists(gfycat_secrets):
            self.logger.warning('Gfycat API keys not found. (See wiki if you need help).')

            # Whitespaces are stripped from input: https://stackoverflow.com/a/3739939
            gfycat_client_id = ''.join(input("[ .. ] Enter Gfycat client ID: ").split())
            gfycat_client_secret = ''.join(input("[ .. ] Enter Gfycat client secret: ").split())
            # Make sure authentication is working
            try:
                gfycat_client = GfycatClient(gfycat_client_id, gfycat_client_secret)

                # If this call doesn't work, it'll throw an ImgurClientError
                gfycat_client.query_gfy('oddyearlyhorsefly')
                # It worked, so save the keys to a file
                gfycat_config = configparser.ConfigParser()
                gfycat_config['Gfycat'] = {'ClientID': gfycat_client_id,
                                           'ClientSecret': gfycat_client_secret,
                                           }
                with open(gfycat_secrets, 'w') as file:
                    gfycat_config.write(file)
                file.close()
            except GfycatClientError as gfycat_error:
                self.logger.error('Error while logging into Gfycat: %s', gfycat_error)
                self.logger.error(FATAL_TOOTBOT_ERROR)
                sys.exit(1)
        else:
            # Read API keys from secret file
            gfycat_config = configparser.ConfigParser()
            gfycat_config.read(gfycat_secrets)

        return gfycat_config

    def _get_imgur_secrets(self, imgur_secrets):
        """
        _get_imgur_secrets checks if the Imgur api secrets file exists.
        - If the file exists, this methods reads the the files and returns the secrets in as a dict.
        - If the file doesn't exist it asks the user over stdin to supply these values and then
          saves them into the imgur_secrets file

        Arguments:
            imgur_secrets (string): file name of secrets file for API credentials

        Returns:
            imgur_config (dict): Dictionary containing the client id and client secret needed to
            login to Imgur
        """

        if not os.path.exists(imgur_secrets):
            self.logger.warning('Imgur API keys not found. (See wiki if you need help).')

            # Whitespaces are stripped from input: https://stackoverflow.com/a/3739939
            imgur_client_id = ''.join(input("[ .. ] Enter Imgur client ID: ").split())
            imgur_client_secret = ''.join(input("[ .. ] Enter Imgur client secret: ").split())
            # Make sure authentication is working
            try:
                imgur_client_id = ImgurClient(imgur_client_id, imgur_client_secret)

                # If this call doesn't work, it'll throw an ImgurClientError
                imgur_client_id.get_album('dqOyj')
                # It worked, so save the keys to a file
                imgur_config = configparser.ConfigParser()
                imgur_config['Imgur'] = {'ClientID': imgur_client_id,
                                         'ClientSecret': imgur_client_secret,
                                         }
                with open(imgur_secrets, 'w') as file:
                    imgur_config.write(file)
                file.close()
            except ImgurClientError as imgur_error:
                self.logger.error('Error while logging into Imgur: %s', imgur_error)
                self.logger.error(FATAL_TOOTBOT_ERROR)
                sys.exit(1)
        else:
            # Read API keys from secret file
            imgur_config = configparser.ConfigParser()
            imgur_config.read(imgur_secrets)

        return imgur_config

    def get_imgur_image(self, img_url):
        """
        get_imgur_image downloads images from imgur.

        Arguments:
            img_url (string): url of imgur image to download

        Returns:
            file_path (string): path to downloaded image or None if no image was downloaded
        """
        # Working demo of regex: https://regex101.com/r/G29uGl/2
        regex = r"(?:.*)imgur\.com(?:\/gallery\/|\/a\/|\/)(.*?)(?:\/.*|\.|$)"
        regex_match = re.search(regex, img_url, flags=0)

        if not regex_match:
            self.logger.error('Could not identify Imgur image/gallery ID at: %s', img_url)
            return None

        # Get the Imgur image/gallery ID
        imgur_id = regex_match.group(1)
        try:
            if any(s in img_url for s in ('/a/', '/gallery/')):  # Gallery links
                images = self.imgur_client.get_album_images(imgur_id)
                # Only the first image in a gallery is used
                imgur_url = images[0].link
            else:  # Single image
                imgur_url = self.imgur_client.get_image(imgur_id).link
        except ImgurClientError as imgur_error:
            self.logger.error('Could not get information from imgur: %s', imgur_error)
            return None

        # If the URL is a GIFV or MP4 link, change it to the GIF version
        file_extension = os.path.splitext(imgur_url)[-1].lower()
        if file_extension == '.gifv':
            file_extension = '.gif'
            imgur_url = imgur_url.replace('.gifv', '.gif')
        elif file_extension == '.mp4':
            file_extension = '.gif'
            imgur_url = imgur_url.replace('.mp4', '.gif')

        # Download the image
        file_path = self.save_dir + '/hr_' + imgur_id + file_extension
        self.logger.info('Downloading Imgur image at URL %s to %s', imgur_url, file_path)
        imgur_file = save_file(imgur_url, file_path, self.logger)

        # Imgur will sometimes return a single-frame thumbnail
        # instead of a GIF, so we need to check for this
        if file_extension == '.gif':
            # Open the file using the Pillow library
            img = Image.open(imgur_file)
            # Get the MIME type
            mime = Image.MIME[img.format]
            img.close()

            # Image is not actually a GIF, so don't post it
            if mime != 'image/gif':
                self.logger.warn('Imgur: not a GIF, not posted to Twitter')
                # Delete the image
                try:
                    os.remove(imgur_file)
                except OSError as os_error:
                    self.logger.error('Error while deleting media file: %s', os_error)
                return None

        return imgur_file

    def get_gfycat_image(self, img_url, low_res=False):
        """
        get_gfycat_image downloads full resolution images from gfycat.

        Arguments:
            img_url (string): url of gfycat image to download
            low_res (boolean): set to True if a low resolution version of the image should be
                downloaded. If False, full resolution image will be downloaded.
                Defaults to False

        Returns:
            file_path (string): path to downloaded image or None if no image was downloaded
        """
        gfycat_url = ""
        file_path = self.save_dir + '/'
        try:
            if low_res:
                gfycat_name = os.path.basename(urlsplit(img_url).path)
                client = self.gfycat_client
                gfycat_info = client.query_gfy(gfycat_name)
                gfycat_url = gfycat_info['gfyItem']['max2mbGif']
                file_path += gfycat_name + '.gif'
            else:
                gfycat_name = os.path.basename(urlsplit(img_url).path)
                response = requests.get(img_url)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'lxml')
                for tag in soup.find_all("source", src=True):
                    src = tag['src']
                    if "giant" in src and "mp4" in src:
                        gfycat_url = src
                file_path += gfycat_name + '.mp4'
        except (requests.ConnectionError,
                requests.Timeout,
                requests.HTTPError,
                GfycatClientError) as gfycat_error:
            self.logger.error('Error downloading Gfycat link: %s', gfycat_error)
            return None

        if gfycat_url == '':
            self.logger.debug('Empty Gfycat URL; no attachment to download')
            return None

        self.logger.info('Downloading Gfycat at URL %s to %s', gfycat_url, file_path)
        return save_file(gfycat_url, file_path, self.logger)

    def get_reddit_image(self, img_url):
        """
        get_reddit_image downloads full resolution images from i.reddit or reddituploads.

        Arguments:
            img_url (string): url of imgur image to download

        Returns:
            file_path (string): path to downloaded image or None if no image was downloaded
        """
        file_name = os.path.basename(urlsplit(img_url).path)
        file_extension = os.path.splitext(img_url)[-2].lower()
        # Fix for issue with i.reddituploads.com links not having a
        # file extension in the URL
        if not file_extension:
            file_extension += '.jpg'
            file_name += '.jpg'
            img_url += '.jpg'
        # Download the file
        file_path = self.save_dir + '/' + file_name
        self.logger.info('Downloading file at URL %s to %s, file type identified as %s',
                         img_url,
                         file_path,
                         file_extension,
                         )
        return save_file(img_url, file_path, self.logger)

    def get_reddit_video(self, reddit_post):
        """
        get_reddit_video downloads full resolution video from i.reddit or reddituploads.

        Arguments:
            reddit_post (reddit_post): reddit post / submission object

        Returns:
            file_path (string): path to downloaded video or None if no image was downloaded
        """
        # Get URL for MP4 version of reddit video
        video_url = reddit_post.media['reddit_video']['fallback_url']
        file_path = self.save_dir + '/hr_' + reddit_post.id + '.mp4'
        self.logger.info('Downloading Reddit video at URL %s to %s', video_url, file_path)
        return save_file(video_url, file_path, self.logger)

    def get_giphy_image(self, img_url, low_res=False):
        """
        get_giphy_image downloads full or low resolution image from giphy

        Arguments:
            img_url (string): url of giphy image to download
            low_res (boolean): set to True if a low resolution version of the image should be
                downloaded. If False, full resolution image will be downloaded.
                Defaults to False

        Returns:
            file_path (string): path to downloaded image or None if no image was downloaded
        """
        # Working demo of regex: https://regex101.com/r/o8m1kA/2
        regex = r"https?://((?:.*)giphy\.com/media/|giphy.com/gifs/|i.giphy.com/)(.*-)?(\w+)(/|\n)"
        match = re.search(regex, img_url, flags=0)
        if not match:
            self.logger.error('Could not identify Giphy ID in this URL: %s', img_url)
            return None

        # Get the Giphy ID
        giphy_id = match.group(3)
        if low_res:
            giphy_url = 'https://media.giphy.com/media/' + giphy_id + '/giphy-downsized.gif'
            file_path = self.save_dir + '/lr_' + giphy_id + '-downsized.gif'
            self.logger.info('Downloading Giphy at %s to %s', giphy_url, file_path)
            giphy_file = save_file(giphy_url, file_path, self.logger)
            # Check the hash to make sure it's not a GIF saying
            # "This content is not available"
            # More info: https://github.com/corbindavenport/tootbot/issues/8
            image_hash = hashlib.md5(open(giphy_file, 'rb').read()).hexdigest()
            if image_hash == '59a41d58693283c72d9da8ae0561e4e5':
                self.logger.warn('Giphy: no 2MB GIF version, not posted to Twitter')
                giphy_file = None
        else:
            # Download the MP4 version of the GIF
            giphy_url = 'https://media.giphy.com/media/' + giphy_id + '/giphy.mp4'
            file_path = self.save_dir + '/hr_' + giphy_id + 'giphy.mp4'
            giphy_file = save_file(giphy_url, file_path, self.logger)
            self.logger.info('Downloading Giphy at URL %s to %s', giphy_url, file_path)

        return giphy_file

    def get_generic_image(self, img_url):
        """
        get_generic_image downloads image or video from a generic url to a media file.

        Arguments:
            img_url (string): url to image or video file

        Returns:
            file_path (string): path to downloaded video or None if no image was downloaded
        """
        # First check if URL starts with http:// or https://
        regex = r"^https?://"
        match = re.search(regex, img_url, flags=0)
        if not match:
            self.logger.info('Post link is not a full link: %s', img_url)
            return None

        # Check if URL is an image or MP4 file, based on the MIME type
        image_formats = ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'video/mp4')
        try:
            img_site = urlopen(img_url)
        except (URLError, UnicodeEncodeError) as url_error:
            self.logger.error('Error while opening URL %s', url_error)
            return None

        meta = img_site.info()
        if meta["content-type"] not in image_formats:
            self.logger.error('URL does not point to a valid image file.')
            return None

        # URL appears to be an image, so download it
        file_name = os.path.basename(urlsplit(img_url).path)
        file_path = self.save_dir + '/' + file_name
        self.logger.info('Downloading file at URL %s to %s', img_url, file_path)
        return save_file(img_url, file_path, self.logger)


class MediaAttachment:
    """
    MediaAttachment contains code to retrieve the appropriate images or videos to include in a
    s reddit post to be shared on Mastodon or Twitter
    """
    LOW_RES = 1
    HIGH_RES = 2
    HIGH_AND_LOW_RES = 3

    def __init__(self, reddit_post, image_helper, download_for, logger):

        self.media_path_low_res = None
        self.media_path_high_res = None
        self.reddit_post = reddit_post
        self.media_url = self.reddit_post.url
        self.image_helper = image_helper
        self.logger = logger

        if download_for in [self.LOW_RES, self.HIGH_AND_LOW_RES]:
            self.media_path_low_res = self._get_media(low_res=True)

        if download_for in [self.HIGH_RES, self.HIGH_AND_LOW_RES]:
            self.media_path_high_res = self._get_media()

        sha256 = hashlib.sha256()
        if self.media_path_low_res is not None:
            with open(self.media_path_low_res, "rb") as media_file:
                # Read and update hash string value in blocks of 4K
                for byte_block in iter(lambda: media_file.read(4096), b""):
                    sha256.update(byte_block)
        self.check_sum_low_res = sha256.hexdigest()

        sha256 = hashlib.sha256()
        if self.media_path_high_res is not None:
            with open(self.media_path_high_res, "rb") as media_file:
                # Read and update hash string value in blocks of 4K
                for byte_block in iter(lambda: media_file.read(4096), b""):
                    sha256.update(byte_block)
        self.check_sum_high_res = sha256.hexdigest()

    def destroy(self):
        """
        Removes any files downloaded and clears out the object attributes.
        """
        try:
            if self.media_path_high_res is not None:
                os.remove(self.media_path_high_res)
                self.logger.info('Deleted media file at %s', self.media_path_high_res)
            if self.media_path_low_res is not None:
                os.remove(self.media_path_low_res)
                self.logger.info('Deleted media file at %s', self.media_path_low_res)
        except OSError as delete_error:
            self.logger.error('Error while deleting media file: %s', delete_error)

        self.media_path_high_res = None
        self.media_path_low_res = None
        self.media_url = None
        self.check_sum_high_res = None

    # Function for obtaining static images and GIFs from popular image hosts
    def _get_media(self, low_res=False):
        if not os.path.exists(self.image_helper.save_dir):
            os.makedirs(self.image_helper.save_dir)
            self.logger.info('Media folder not found, created new folder: %s',
                             self.image_helper.save_dir)

        file_path = None

        # Download and save the linked image
        if any(s in self.media_url for s in ('i.redd.it', 'i.reddituploads.com')):  # Reddit-hosted
            file_path = self.image_helper.get_reddit_image(self.media_url)

        elif 'v.redd.it' in self.media_url and low_res:  # Reddit video
            self.logger.warn('Videos can not be uploaded to Twitter, due to API limitations')
        elif 'v.redd.it' in self.media_url and not self.reddit_post.media:  # Reddit video
            self.logger.error('Reddit API returned no media for this URL: %s', self.media_url)
        elif 'v.redd.it' in self.media_url:
            file_path = self.image_helper.get_reddit_video(self.reddit_post)

        elif 'imgur.com' in self.media_url:
            file_path = self.image_helper.get_imgur_image(self.media_url)

        elif 'gfycat.com' in self.media_url:  # Gfycat
            file_path = self.image_helper.get_gfycat_image(self.media_url, low_res=low_res)

        elif 'giphy.com' in self.media_url:  # Giphy
            file_path = self.image_helper.get_giphy_image(self.media_url, low_res=low_res)

        else:
            file_path = self.image_helper.get_generic_image(self.media_url)

        return file_path
