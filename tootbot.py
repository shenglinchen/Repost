import configparser
import distutils.util
import logging
import os
import sys
import time

import coloredlogs
import requests
import tweepy
from mastodon import Mastodon, MastodonError
from requests.exceptions import ConnectionError, HTTPError, Timeout

from collect import LinkedMediaHelper
from collect import MediaAttachment
from collect import RedditHelper
from control import PostRecorder
from monitoring import HealthChecks
from publish import MastodonPublisher

MAX_LEN_TWEET = 280
MAX_LEN_TOOT = 500
CODE_VERSION_MAJOR = 2  # Current major version of this code
CODE_VERSION_MINOR = 15  # Current minor version of this code
CODE_VERSION_PATCH = 3  # Current patch version of this code


def get_caption(submission, max_len, addhashtags=None):
    global NUM_NON_PROMO_MESSAGES
    global PROMO_EVERY
    # Create string of hashtags
    hashtag_string = ''
    promo_string = ''

    logger.debug('HASHTAGS: %s', HASHTAGS)
    logger.debug('addhashtags: %s', addhashtags)

    if addhashtags is None:
        hashtags_for_post = HASHTAGS
    else:
        hashtags_for_post = [x.strip() for x in addhashtags.split(',')]
        if HASHTAGS is not None:
            hashtags_for_post += HASHTAGS

    if hashtags_for_post:
        for tag in hashtags_for_post:
            # Add hashtag to string, followed by a space for the next one
            hashtag_string += '#' + tag + ' '
    # Set the Mastodon max title length for 500, minus the length of the
    # shortlink and hashtags, minus one for the space between title
    # and shortlink
    if 0 < PROMO_EVERY <= NUM_NON_PROMO_MESSAGES:
        promo_string = ' \n \n%s' % PROMO_MESSAGE
        NUM_NON_PROMO_MESSAGES = 0
    caption_max_length = max_len - len(
        submission.shortlink) - len(hashtag_string) - len(promo_string) - 1
    # Create contents of the Mastodon post
    if len(submission.title) < caption_max_length:
        caption = submission.title + ' '
    else:
        caption = submission.title[caption_max_length - 3] + '... '
    caption += hashtag_string + submission.shortlink + promo_string
    return caption


def make_post(posts, duplicate_checker, media_helper):
    global NUM_NON_PROMO_MESSAGES
    break_to_mainloop = False
    for additional_hashtags, source_posts in posts.items():
        if break_to_mainloop:
            break

        for post in source_posts:
            # Grab post details from dictionary
            post_id = source_posts[post].id
            shared_url = source_posts[post].url
            if not (duplicate_checker.duplicate_check(post_id) or
                    duplicate_checker.duplicate_check(shared_url)):
                logger.debug('Processing reddit post: %s', source_posts[post])
                # Post on Twitter
                if POST_TO_TWITTER:
                    # Download Twitter-compatible version of media file
                    # (static image or GIF under 3MB)
                    media_attachment = MediaAttachment(source_posts[post],
                                                       media_helper,
                                                       MediaAttachment.LOW_RES,
                                                       logger,
                                                       )
                    # Make sure the post contains media,
                    # if MEDIA_POSTS_ONLY in config is set to True
                    if (((MEDIA_POSTS_ONLY is True) and media_attachment.media_path_low_res) or
                            (MEDIA_POSTS_ONLY is False)):
                        try:
                            twitter_auth = tweepy.OAuthHandler(CONSUMER_KEY,
                                                               CONSUMER_SECRET)
                            twitter_auth.set_access_token(ACCESS_TOKEN,
                                                          ACCESS_TOKEN_SECRET)
                            twitter_api = tweepy.API(twitter_auth)
                            NUM_NON_PROMO_MESSAGES += 1
                            # Generate post caption
                            caption = get_caption(source_posts[post], MAX_LEN_TWEET,
                                                  addhashtags=additional_hashtags)
                            # Post the tweet
                            if media_attachment.media_path_low_res:
                                logger.info('Posting this on Twitter with media %s', caption)
                                tweet = twitter_api.update_with_media(
                                    filename=media_attachment.media_path_low_res,
                                    status=caption)

                                duplicate_checker.log_post(post_id,
                                                           'https://twitter.com/' +
                                                           twitter_username + '/status/'
                                                           + tweet.id_str + '/',
                                                           shared_url,
                                                           media_attachment.check_sum_low_res,
                                                           )
                                media_attachment.destroy()
                            else:
                                logger.info('Posting this on Twitter: %s', caption)
                                tweet = twitter_api.update_status(status=caption)
                                duplicate_checker.log_post(post_id,
                                                           'https://twitter.com/' +
                                                           twitter_username + '/status/'
                                                           + tweet.id_str + '/',
                                                           shared_url,
                                                           '',
                                                           )
                            # Log the tweet
                        except BaseException as e:
                            logger.error('Error while posting tweet: %s', e)
                            duplicate_checker.log_post(post_id, 'Error wile posting tweet: %s' % e,
                                                       '', '')
                        logger.warning(
                            'Twitter: Skipping %s because non-media posts are disabled or the media file was not found',
                            post_id)
                        # Log the post anyways
                        duplicate_checker.log_post(
                            post_id,
                            'Twitter: Skipped because non-media posts are disabled or the media file was not found',
                            '',
                            ''
                        )

                # Post on Mastodon
                if MASTODON_INSTANCE_DOMAIN:

                    attachment = MediaAttachment(source_posts[post],
                                                 media_helper,
                                                 MediaAttachment.HIGH_RES,
                                                 logger
                                                 )
                    logger.debug('Attachement media_path ->%s<-', attachment.media_path_high_res)

                    # Check for duplicate of attachment sha256
                    if attachment.media_path_high_res and \
                            duplicate_checker.duplicate_check(attachment.check_sum_high_res):
                        logger.info(
                            'Skipping %s because attachment with hash %s has already been posted',
                            post_id, attachment.check_sum_high_res)
                        duplicate_checker.log_post(post_id,
                                                   'Mastodon: Skipped because image with hash has already been posted',
                                                   '',
                                                   attachment.check_sum_high_res)
                        attachment.destroy()
                        continue

                    logger.debug('MEDIA_POSTS_ONLY: %s', MEDIA_POSTS_ONLY)

                    # Make sure the post contains media,
                    # if MEDIA_POSTS_ONLY in config is set to True
                    if (((MEDIA_POSTS_ONLY is True) and attachment.media_path_high_res)
                            or (MEDIA_POSTS_ONLY is False)):
                        try:
                            NUM_NON_PROMO_MESSAGES += 1
                            # Generate post caption
                            caption = get_caption(source_posts[post], MAX_LEN_TOOT,
                                                  addhashtags=additional_hashtags)
                            # Post the toot
                            if attachment.media_path_high_res:
                                logger.info('Posting this on Mastodon with media: %s', caption)
                                logger.info('High Res Media checksum: %s',
                                            attachment.check_sum_high_res)
                                media = mastodon.media_post(attachment.media_path_high_res,
                                                            mime_type=None)
                                # If the post is marked as NSFW on Reddit,
                                # force sensitive media warning for images
                                if source_posts[post].over_18 and NSFW_POSTS_MARKED:
                                    toot = mastodon.status_post(caption, media_ids=[media],
                                                                spoiler_text='NSFW')
                                else:
                                    toot = mastodon.status_post(
                                        caption,
                                        media_ids=[media],
                                        sensitive=MASTODON_SENSITIVE_MEDIA)

                            else:
                                logger.info('Posting this on Mastodon: %s', caption)
                                # Add NSFW warning for Reddit posts marked as NSFW
                                if source_posts[post].over_18:
                                    toot = mastodon.status_post(caption, spoiler_text='NSFW')
                                else:
                                    toot = mastodon.status_post(caption)
                            # Log the toot
                            duplicate_checker.log_post(post_id, toot["url"], shared_url,
                                                       attachment.check_sum_high_res)
                        except MastodonError as toot_error:
                            logger.error('Error while posting toot: %s', toot_error)
                            # Log the post anyways
                            duplicate_checker.log_post(post_id,
                                                       'Error while posting toot: %s' % toot_error,
                                                       '',
                                                       '')

                    else:
                        logger.warning(
                            'Mastodon: Skipping %s because non-media posts are disabled or media file was not found',
                            post_id)
                        # Log the post anyways
                        duplicate_checker.log_post(
                            post_id,
                            'Mastodon: Skipped because non-media posts are disabled or the media file was not found',
                            '',
                            ''
                        )

                    # Clean up media file
                    attachment.destroy()

                # Return control to main loop
                break_to_mainloop = True
                break

            logger.info('Skipping %s because it was already posted', post_id)


# Make sure config file exists
try:
    config = configparser.ConfigParser()
    config.read('config.ini')
except configparser.Error as config_error:
    print('[ERROR] Error while reading config file: %s', config_error)
    sys.exit()

LOG_LEVEL = 'INFO'
if config['BotSettings']['LogLevel']:
    LOG_LEVEL = config['BotSettings']['LogLevel']

# Set-up logging
logger = logging.getLogger(__name__)
coloredlogs.install(
    level=LOG_LEVEL,
    fmt='%(asctime)s %(name)s[%(process)d] %(levelname)s %(message)s',
    datefmt='%H:%M:%S')

# General settings
post_recorder = PostRecorder(config['BotSettings']['CacheFile'], logger)
DELAY_BETWEEN_TWEETS = int(config['BotSettings']['DelayBetweenPosts'])
RUN_ONCE_ONLY = bool(
    distutils.util.strtobool(config['BotSettings']['RunOnceOnly']))
POST_LIMIT = int(config['BotSettings']['PostLimit'])
NSFW_POSTS_ALLOWED = bool(
    distutils.util.strtobool(config['BotSettings']['NSFWPostsAllowed']))
NSFW_POSTS_MARKED = bool(
    distutils.util.strtobool(config['BotSettings']['NSFWPostsMarked']))
SPOILERS_ALLOWED = bool(
    distutils.util.strtobool(config['BotSettings']['SpoilersAllowed']))
SELF_POSTS_ALLOWED = bool(
    distutils.util.strtobool(config['BotSettings']['SelfPostsAllowed']))
if config['BotSettings']['Hashtags']:
    # Parse list of hashtags
    HASHTAGS = config['BotSettings']['Hashtags']
    HASHTAGS = [x.strip() for x in HASHTAGS.split(',')]
else:
    HASHTAGS = None
# Settings related to promotional messages
PROMO_EVERY = int(config['PromoSettings']['PromoEvery'])
PROMO_MESSAGE = config['PromoSettings']['PromoMessage']
# HealthChecks related settings
DO_HEALTHCHECKS = False
hc_base_url = config['HealthChecks']['BaseUrl']
hc_uid = config['HealthChecks']['UID']
if len(hc_base_url) > 0:
    DO_HEALTHCHECKS = True
# Settings related to media attachments
MEDIA_POSTS_ONLY = bool(
    distutils.util.strtobool(config['MediaSettings']['MediaPostsOnly']))
IMAGE_DIR = config['MediaSettings']['MediaFolder']
# Twitter info
POST_TO_TWITTER = bool(
    distutils.util.strtobool(config['Twitter']['PostToTwitter']))
# Mastodon info
MASTODON_INSTANCE_DOMAIN = config['Mastodon']['InstanceDomain']
POST_TO_MASTODON = bool(
    distutils.util.strtobool(config['Mastodon']['PostToMastodon']))
MASTODON_SENSITIVE_MEDIA = bool(
    distutils.util.strtobool(config['Mastodon']['SensitiveMedia']))
MASTODON_DELETE_AFTER_DAYS = int(config['Mastodon']['DeleteAfterDays'])

SUBREDDITS = config.items('Subreddits')


POST_TO_REDDIT = bool(
    distutils.util.strtobool(config['Reddit']['PostToReddit']))

# Check for updates
try:
    response = requests.get(
        'https://gitlab.com/marvin8/tootbot/-/raw/main/update-check/release-version.txt')
    response.raise_for_status()
    repo_version = response.content.decode('utf-8').strip().partition('.')
    repo_version_major = int(repo_version[0].strip())
    repo_minor_version_to_check = repo_version[2].strip().partition('.')
    if repo_minor_version_to_check[1] == '':
        repo_version_minor = int(repo_minor_version_to_check[0].strip())
        repo_version_patch = 0
    else:
        repo_version_minor = int(repo_minor_version_to_check[0].strip())
        repo_version_patch = int(repo_minor_version_to_check[2].strip())
    if CODE_VERSION_MAJOR >= repo_version_major and \
            CODE_VERSION_MINOR >= repo_version_minor and \
            CODE_VERSION_PATCH >= repo_version_patch:
        logger.info('You have the latest version of Tootbot (%s.%s.%s)', CODE_VERSION_MAJOR,
                    CODE_VERSION_MINOR, CODE_VERSION_PATCH)
    else:
        logger.warning('A new version of Tootbot (%s.%s.%s) is available! (you have %s.%s.%s)',
                       repo_version_major, repo_version_minor, repo_version_patch,
                       CODE_VERSION_MAJOR, CODE_VERSION_MINOR, CODE_VERSION_PATCH)
        logger.warning('Get the latest update from here: https://gitlab.com/marvin8/tootbot/')
except (ConnectionError, Timeout, HTTPError) as update_check_error:
    logger.info('while checking for updates we got this error: %s', update_check_error)

# Log into Twitter if enabled in settings
if POST_TO_TWITTER is True:
    if os.path.exists('twitter.secret'):
        # Read API keys from secret file
        twitter_config = configparser.ConfigParser()
        twitter_config.read('twitter.secret')
        ACCESS_TOKEN = twitter_config['Twitter']['AccessToken']
        ACCESS_TOKEN_SECRET = twitter_config['Twitter']['AccessTokenSecret']
        CONSUMER_KEY = twitter_config['Twitter']['ConsumerKey']
        CONSUMER_SECRET = twitter_config['Twitter']['ConsumerSecret']
        try:
            # Make sure authentication is working
            test_twitter_auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
            test_twitter_auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
            twitter = tweepy.API(test_twitter_auth)
            twitter_username = twitter.me().screen_name
            logger.info('Successfully authenticated on Twitter as @%s', twitter_username)
        except BaseException as mastodon_error:
            logger.error('Error while logging into Twitter: %s', mastodon_error)
            logger.error('Tootbot cannot continue, now shutting down')
            sys.exit(1)
    else:
        # If the secret file doesn't exist, it means the setup process
        # hasn't happened yet
        logger.warning('Twitter API keys not found. (See wiki for help).')
        # Whitespaces are stripped from input:
        # https://stackoverflow.com/a/3739939
        ACCESS_TOKEN = ''.join(input('[ .. ] Enter access token for Twitter account: ').split())
        ACCESS_TOKEN_SECRET = ''.join(
            input('[ .. ] Enter access token secret for Twitter account: ').split())
        CONSUMER_KEY = ''.join(input('[ .. ] Enter consumer key for Twitter account: ').split())
        CONSUMER_SECRET = ''.join(
            input('[ .. ] Enter consumer secret for Twitter account: ').split())
        logger.info('Attempting to log in to Twitter...')
        try:
            # Make sure authentication is working
            test_twitter_auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
            test_twitter_auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
            twitter = tweepy.API(test_twitter_auth)
            twitter_username = twitter.me().screen_name
            logger.info('Successfully authenticated on Twitter as @%s', twitter_username)
            # It worked, so save the keys to a file
            twitter_config = configparser.ConfigParser()
            twitter_config['Twitter'] = {
                'AccessToken': ACCESS_TOKEN,
                'AccessTokenSecret': ACCESS_TOKEN_SECRET,
                'ConsumerKey': CONSUMER_KEY,
                'ConsumerSecret': CONSUMER_SECRET
            }
            with open('twitter.secret', 'w') as f:
                twitter_config.write(f)
            f.close()
        except BaseException as mastodon_error:
            logger.error('Error while logging into Twitter: %s', mastodon_error)
            logger.error('Tootbot cannot continue, now shutting down')
            sys.exit(1)
# Log into Mastodon if enabled in settings
if POST_TO_MASTODON is True:
    if not os.path.exists('mastodon.secret'):
        # If the secret file doesn't exist,
        # it means the setup process hasn't happened yet
        logger.warning('Mastodon API keys not found. (See wiki for help).')
        MASTODON_USERNAME = input(
            "[ .. ] Enter email address for Mastodon account: ")
        MASTODON_PASSWORD = input(
            "[ .. ] Enter password for Mastodon account: ")
        logger.info('Generating login key for Mastodon...')
        try:
            Mastodon.create_app(
                'Tootbot',
                website='https://gitlab.com/marvin8/tootbot',
                api_base_url='https://' + MASTODON_INSTANCE_DOMAIN,
                to_file='mastodon.secret')
            mastodon = Mastodon(client_id='mastodon.secret',
                                api_base_url='https://' +
                                             MASTODON_INSTANCE_DOMAIN)
            mastodon.log_in(MASTODON_USERNAME,
                            MASTODON_PASSWORD,
                            to_file='mastodon.secret')
            # Make sure authentication is working
            mastodon_user = mastodon.account_verify_credentials()
            mastodon_username = mastodon_user['username']
            logger.info(
                'Successfully authenticated on %s as @%s login information now stored in mastodon.secret file',
                MASTODON_INSTANCE_DOMAIN, mastodon_username)
        except MastodonError as mastodon_error:
            logger.error('Error while logging into Mastodon: %s', mastodon_error)
            logger.error('Tootbot cannot continue, now shutting down')
            sys.exit(1)
    else:
        try:
            mastodon = Mastodon(access_token='mastodon.secret',
                                api_base_url='https://' +
                                             MASTODON_INSTANCE_DOMAIN)
            # Make sure authentication is working
            mastodon_user = mastodon.account_verify_credentials()
            mastodon_username = mastodon_user['username']
            logger.info('Successfully authenticated on %s as @%s', MASTODON_INSTANCE_DOMAIN,
                        mastodon_username)
        except MastodonError as mastodon_error:
            logger.error('Error while logging into Mastodon: %s', mastodon_error)
            logger.error('Tootbot cannot continue, now shutting down')
            sys.exit(1)
# 
# Set the command line window title on Windows
if os.name == 'nt':
    try:
        if POST_TO_TWITTER and POST_TO_MASTODON:
            # Set title with both Twitter and Mastodon usernames
            # twitter_username = twitter.me().screen_name
            os.system('title ' + twitter_username + '@twitter.com and ' +
                      mastodon_username + '@' + MASTODON_INSTANCE_DOMAIN +
                      ' - Tootbot')
        elif POST_TO_TWITTER:
            # Set title with just Twitter username
            twitter_username = twitter.me().screen_name
            os.system('title ' + '@' + twitter_username + ' - Tootbot')
        elif POST_TO_MASTODON:
            # Set title with just Mastodon username
            os.system('title ' + mastodon_username + '@' +
                      MASTODON_INSTANCE_DOMAIN + ' - Tootbot')
    except OSError:
        os.system('title Tootbot')

# Setup healthcheck monitoring
healthcheck = HealthChecks(base_url=hc_base_url,
                           uid=hc_uid,
                           logger=logger)

reddit = RedditHelper(logger=logger)
reddit.allow_nsfw = NSFW_POSTS_ALLOWED
reddit.allow_self = SELF_POSTS_ALLOWED
reddit.allow_spoilers = SPOILERS_ALLOWED

imgur = LinkedMediaHelper(IMAGE_DIR, logger)

mastodon_publisher = MastodonPublisher(mastodon, mastodon_user, logger)

# Run the main script
NUM_NON_PROMO_MESSAGES = 0  # type: int
while True:
    if DO_HEALTHCHECKS:
        healthcheck.check_start()

    reddit_posts = {}
    for subreddit, hashtags in SUBREDDITS:
        reddit_posts[hashtags] = reddit.get_reddit_posts(subreddit, limit=POST_LIMIT)
    make_post(reddit_posts, post_recorder, imgur)

    if MASTODON_DELETE_AFTER_DAYS > 0:
        logger.info('Deleting Toots older than %s days', MASTODON_DELETE_AFTER_DAYS)
        mastodon_publisher.delete_toots(older_than_days=MASTODON_DELETE_AFTER_DAYS)
    else:
        logger.info('Deleting old toots disabled')

    if DO_HEALTHCHECKS:
        healthcheck.check_ok()

    if RUN_ONCE_ONLY:
        logger.info('Exiting because RunOnceOnly is set to %s', RUN_ONCE_ONLY)
        sys.exit()

    logger.info('Sleeping for %s seconds', DELAY_BETWEEN_TWEETS)
    time.sleep(DELAY_BETWEEN_TWEETS)
    logger.info('Restarting main process...')
