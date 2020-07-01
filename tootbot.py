import configparser
import csv
import distutils.core
import logging
import os
import sys
import time
import urllib

import coloredlogs
import praw
import tweepy
from imgurpython import ImgurClient
from mastodon import Mastodon

from getmedia import MediaAttachment
from getmedia import get_media

MAX_LEN_TWEET = 280
MAX_LEN_TOOT = 500


def get_reddit_posts(subreddit_info):
    posts = {}
    logger.info('Getting posts from Reddit...')
    for submission in subreddit_info.hot(limit=POST_LIMIT):
        if submission.over_18 and NSFW_POSTS_ALLOWED is False:
            # Skip over NSFW posts if they are disabled in the config file
            logger.info('Skipping %s because it is marked as NSFW' % submission.id)
            continue
        elif submission.is_self and SELF_POSTS_ALLOWED is False:
            # Skip over NSFW posts if they are disabled in the config file
            logger.info('Skipping %s because it is a self post' % submission.id)
            continue
        elif submission.spoiler and SPOILERS_ALLOWED is False:
            # Skip over posts marked as spoilers if they are disabled in
            # the config file
            logger.info('Skipping %s because it is marked as a spoiler' % submission.id)
            continue
        elif submission.stickied:
            logger.info('Skipping %s because it is stickied' % submission.id)
            continue
        else:
            # Create dict
            posts[submission.id] = submission
    return posts


def get_caption(submission, max_len):
    global NUM_NON_PROMO_MESSAGES
    global PROMO_EVERY
    # Create string of hashtags
    hashtag_string = ''
    promo_string = ''
    if HASHTAGS:
        for tag in HASHTAGS:
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


def setup_connection_reddit(subreddit):
    logger.info('Setting up connection with Reddit...')
    r = praw.Reddit(user_agent='Tootbot',
                    client_id=REDDIT_AGENT,
                    client_secret=REDDIT_CLIENT_SECRET)
    return r.subreddit(subreddit)


def duplicate_check(identifier):
    value = False
    with open(CACHE_CSV, 'rt', newline='') as cache_file:
        reader = csv.reader(cache_file, delimiter=',')
        for row in reader:
            if identifier in row:
                value = True
    cache_file.close()
    return value


def log_post(reddit_id, post_url, shared_url, check_sum):
    with open(CACHE_CSV, 'a', newline='') as cache_file:
        date = time.strftime("%d/%m/%Y") + ' ' + time.strftime("%H:%M:%S")
        cache_csv_writer = csv.writer(cache_file, delimiter=',')
        cache_csv_writer.writerow([reddit_id, date, post_url, shared_url, check_sum])
    cache_file.close()


def make_post(source_posts):
    global NUM_NON_PROMO_MESSAGES
    for post in source_posts:
        # Grab post details from dictionary
        post_id = source_posts[post].id
        shared_url = source_posts[post].url
        if not (duplicate_check(post_id) or duplicate_check(shared_url)):
            logger.debug('Processing reddit post: %s' % (source_posts[post]))
            # Post on Twitter
            if POST_TO_TWITTER:
                # Download Twitter-compatible version of media file
                # (static image or GIF under 3MB)
                media_file = get_media(shared_url, IMGUR_CLIENT, IMGUR_CLIENT_SECRET, IMAGE_DIR, logger)
                # Make sure the post contains media,
                # if MEDIA_POSTS_ONLY in config is set to True
                if (((MEDIA_POSTS_ONLY is True) and media_file) or
                        (MEDIA_POSTS_ONLY is False)):
                    try:
                        twitter_auth = tweepy.OAuthHandler(CONSUMER_KEY,
                                                           CONSUMER_SECRET)
                        twitter_auth.set_access_token(ACCESS_TOKEN,
                                                      ACCESS_TOKEN_SECRET)
                        twitter_api = tweepy.API(twitter_auth)
                        NUM_NON_PROMO_MESSAGES += 1
                        # Generate post caption
                        caption = get_caption(source_posts[post], MAX_LEN_TWEET)
                        # Post the tweet
                        if media_file:
                            logger.info('Posting this on Twitter with media %s' % caption)
                            tweet = twitter_api.update_with_media(filename=media_file, status=caption)
                            # Clean up media file
                            try:
                                os.remove(media_file)
                                logger.info('Deleted media file at %s' % media_file)
                            except BaseException as e:
                                logger.error('Error while deleting media file: %s' % e)
                        else:
                            logger.info('Posting this on Twitter: %s' % caption)
                            tweet = twitter_api.update_status(status=caption)
                        # Log the tweet
                        log_post(
                            post_id, 'https://twitter.com/' +
                                     twitter_username + '/status/' + tweet.id_str + '/',
                            shared_url,
                            '')
                    except BaseException as e:
                        logger.error('Error while posting tweet: %s' % e)
                        # Log the post anyways
                        log_post(post_id, 'Error while posting tweet: %s' % e, '', '')
                else:
                    logger.warning(
                        'Twitter: Skipping %s because non-media posts are disabled or the media file was not found'
                        % post_id)
                    # Log the post anyways
                    log_post(
                        post_id,
                        'Twitter: Skipped because non-media posts are disabled or the media file was not found',
                        '',
                        ''
                    )

            # Post on Mastodon
            if MASTODON_INSTANCE_DOMAIN:
                # Download Mastodon-compatible version of media file
                # (static image or MP4 file)
                attachment = MediaAttachment(source_posts[post],
                                             IMGUR_CLIENT,
                                             IMGUR_CLIENT_SECRET,
                                             IMAGE_DIR,
                                             MediaAttachment.HIGH_RES,
                                             logger
                                             )
                # Duplicate check with hash
                if duplicate_check(attachment.check_sum_high_res):
                    logger.info('Skipping %s because image was already posted' % post_id)
                    log_post(post_id,
                             'Skipping post as image has already been posted',
                             shared_url,
                             attachment.check_sum_high_res)
                    return

                # Make sure the post contains media,
                # if MEDIA_POSTS_ONLY in config is set to True
                if (((MEDIA_POSTS_ONLY is True) and attachment.media_path_high_res)
                        or (MEDIA_POSTS_ONLY is False)):
                    try:
                        NUM_NON_PROMO_MESSAGES += 1
                        # Generate post caption
                        caption = get_caption(source_posts[post], MAX_LEN_TOOT)
                        # Post the toot
                        if attachment.media_path_high_res:
                            logger.info('Posting this on Mastodon with media: %s' % caption)
                            logger.info('High Res Media checksum: %s' % attachment.check_sum_high_res)
                            media = mastodon.media_post(attachment.media_path_high_res, mime_type=None)
                            # If the post is marked as NSFW on Reddit,
                            # force sensitive media warning for images
                            if source_posts[post].over_18 and NSFW_POSTS_MARKED:
                                toot = mastodon.status_post(caption, media_ids=[media], spoiler_text='NSFW')
                            else:
                                toot = mastodon.status_post(
                                    caption,
                                    media_ids=[media],
                                    sensitive=MASTODON_SENSITIVE_MEDIA)

                        else:
                            logger.info('Posting this on Mastodon: %s' % caption)
                            # Add NSFW warning for Reddit posts marked as NSFW
                            if source_posts[post].over_18:
                                toot = mastodon.status_post(caption, spoiler_text='NSFW')
                            else:
                                toot = mastodon.status_post(caption)
                        # Log the toot
                        log_post(post_id, toot["url"], shared_url, attachment.check_sum_high_res)
                    except BaseException as e:
                        logger.error('Error while posting toot: %s' % e)
                        # Log the post anyways
                        log_post(post_id, 'Error while posting toot: %s' % e, '', '')

                    # Clean up media file
                    attachment.destroy(logger)
                else:
                    logger.warning(
                        'Mastodon: Skipping %s because non-media posts are disabled or the media file was not found'
                        % post_id)
                    # Log the post anyways
                    log_post(
                        post_id,
                        'Mastodon: Skipped because non-media posts are disabled or the media file was not found',
                        '',
                        ''
                    )

            # Go to sleep
            logger.info('Sleeping for %s seconds' % DELAY_BETWEEN_TWEETS)
            time.sleep(DELAY_BETWEEN_TWEETS)
        else:
            logger.info('Skipping %s because it was already posted' % post_id)


# Make sure config file exists
try:
    config = configparser.ConfigParser()
    config.read('config.ini')
except BaseException as e:
    print('[ERROR] Error while reading config file: %s' % e)
    sys.exit()
# General settings
CACHE_CSV = config['BotSettings']['CacheFile']
DELAY_BETWEEN_TWEETS = int(config['BotSettings']['DelayBetweenPosts'])
RUN_ONCE_ONLY = bool(
    distutils.util.strtobool(config['BotSettings']['RunOnceOnly']))
POST_LIMIT = int(config['BotSettings']['PostLimit'])
SUBREDDIT_TO_MONITOR = config['BotSettings']['SubredditToMonitor']
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
    HASHTAGS = ''
LOG_LEVEL = 'INFO'
if config['BotSettings']['LogLevel']:
    LOG_LEVEL = config['BotSettings']['LogLevel']
# Settings related to promotional messages
PROMO_EVERY = int(config['PromoSettings']['PromoEvery'])
PROMO_MESSAGE = config['PromoSettings']['PromoMessage']
# Settings related to media attachments
MEDIA_POSTS_ONLY = bool(
    distutils.util.strtobool(config['MediaSettings']['MediaPostsOnly']))
IMAGE_DIR = config['MediaSettings']['MediaFolder']
# Twitter info
POST_TO_TWITTER = bool(
    distutils.util.strtobool(config['Twitter']['PostToTwitter']))
# Mastodon info
MASTODON_INSTANCE_DOMAIN = config['Mastodon']['InstanceDomain']
MASTODON_SENSITIVE_MEDIA = bool(
    distutils.util.strtobool(config['Mastodon']['SensitiveMedia']))

# Set-up logging
logger = logging.getLogger(__name__)
coloredlogs.install(
    level=LOG_LEVEL,
    fmt='%(asctime)s %(name)s[%(process)d] %(levelname)s %(message)s',
    datefmt='%H:%M:%S')

# Check for updates
try:
    with urllib.request.urlopen(
            "https://raw.githubusercontent.com/corbindavenport/tootbot/update-check/current-version.txt"
    ) as url:
        s = url.read()
        new_version = s.decode("utf-8").rstrip()
        current_version = 2.7  # Current version of script
        if current_version < float(new_version):
            logger.warning('A new version of Tootbot (' + str(new_version) +
                           ') is available! (you have ' +
                           str(current_version) + ')')
            logger.warning(
                'Get the latest update from here: https://github.com/corbindavenport/tootbot/releases'
            )
        else:
            logger.info('You have the latest version of Tootbot (' + str(current_version) + ')')
    url.close()
except BaseException as e:
    logger.error('Error while checking for updates: %s' % e)

# Setup and verify Reddit access
if not os.path.exists('reddit.secret'):
    logger.warning('Reddit API keys not found. (See wiki if you need help).')
    # Whitespaces are stripped from input: https://stackoverflow.com/a/3739939
    REDDIT_AGENT = ''.join(input("[ .. ] Enter Reddit agent: ").split())
    REDDIT_CLIENT_SECRET = ''.join(
        input("[ .. ] Enter Reddit client secret: ").split())
    # Make sure authentication is working
    try:
        reddit_client = praw.Reddit(user_agent='Tootbot', client_id=REDDIT_AGENT, client_secret=REDDIT_CLIENT_SECRET)
        test = reddit_client.subreddit('announcements')
        # It worked, so save the keys to a file
        reddit_config = configparser.ConfigParser()
        reddit_config['Reddit'] = {'Agent': REDDIT_AGENT, 'ClientSecret': REDDIT_CLIENT_SECRET}
        with open('reddit.secret', 'w') as f:
            reddit_config.write(f)
        f.close()
    except BaseException as e:
        logger.error('Error while logging into Reddit: %s' % e)
        logger.error('Tootbot cannot continue, now shutting down')
        exit()
else:
    # Read API keys from secret file
    reddit_config = configparser.ConfigParser()
    reddit_config.read('reddit.secret')
    REDDIT_AGENT = reddit_config['Reddit']['Agent']
    REDDIT_CLIENT_SECRET = reddit_config['Reddit']['ClientSecret']
# Setup and verify Imgur access
if not os.path.exists('imgur.secret'):
    logger.warning(
        'Imgur API keys not found. (See wiki if you need help).'
    )
    # Whitespaces are stripped from input: https://stackoverflow.com/a/3739939
    IMGUR_CLIENT = ''.join(input("[ .. ] Enter Imgur client ID: ").split())
    IMGUR_CLIENT_SECRET = ''.join(input("[ .. ] Enter Imgur client secret: ").split())
    # Make sure authentication is working
    try:
        imgur_client = ImgurClient(IMGUR_CLIENT, IMGUR_CLIENT_SECRET)
        test_gallery = imgur_client.get_album('dqOyj')
        # It worked, so save the keys to a file
        imgur_config = configparser.ConfigParser()
        imgur_config['Imgur'] = {
            'ClientID': IMGUR_CLIENT,
            'ClientSecret': IMGUR_CLIENT_SECRET
        }
        with open('imgur.secret', 'w') as f:
            imgur_config.write(f)
        f.close()
    except BaseException as e:
        logger.error('Error while logging into Imgur: %s' % e)
        logger.error('Tootbot cannot continue, now shutting down')
        exit()
else:
    # Read API keys from secret file
    imgur_config = configparser.ConfigParser()
    imgur_config.read('imgur.secret')
    IMGUR_CLIENT = imgur_config['Imgur']['ClientID']
    IMGUR_CLIENT_SECRET = imgur_config['Imgur']['ClientSecret']
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
            logger.info('Successfully authenticated on Twitter as @' +
                        twitter_username)
        except BaseException as e:
            logger.error('Error while logging into Twitter: %s' % e)
            logger.error('Tootbot cannot continue, now shutting down')
            exit()
    else:
        # If the secret file doesn't exist, it means the setup process
        # hasn't happened yet
        logger.warning('Twitter API keys not found. (See wiki for help).')
        # Whitespaces are stripped from input:
        # https://stackoverflow.com/a/3739939
        ACCESS_TOKEN = ''.join(input('[ .. ] Enter access token for Twitter account: ').split())
        ACCESS_TOKEN_SECRET = ''.join(input('[ .. ] Enter access token secret for Twitter account: ').split())
        CONSUMER_KEY = ''.join(input('[ .. ] Enter consumer key for Twitter account: ').split())
        CONSUMER_SECRET = ''.join(input('[ .. ] Enter consumer secret for Twitter account: ').split())
        logger.info('Attempting to log in to Twitter...')
        try:
            # Make sure authentication is working
            test_twitter_auth = tweepy.OAuthHandler(CONSUMER_KEY, CONSUMER_SECRET)
            test_twitter_auth.set_access_token(ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
            twitter = tweepy.API(test_twitter_auth)
            twitter_username = twitter.me().screen_name
            logger.info('Successfully authenticated on Twitter as @' +
                        twitter_username)
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
        except BaseException as e:
            logger.error('Error while logging into Twitter: %s' % e)
            logger.error('Tootbot cannot continue, now shutting down')
            exit()
# Log into Mastodon if enabled in settings
if MASTODON_INSTANCE_DOMAIN:
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
                website='https://github.com/corbindavenport/tootbot',
                api_base_url='https://' + MASTODON_INSTANCE_DOMAIN,
                to_file='mastodon.secret')
            mastodon = Mastodon(client_id='mastodon.secret',
                                api_base_url='https://' +
                                             MASTODON_INSTANCE_DOMAIN)
            mastodon.log_in(MASTODON_USERNAME,
                            MASTODON_PASSWORD,
                            to_file='mastodon.secret')
            # Make sure authentication is working
            mastodon_username = mastodon.account_verify_credentials()['username']
            logger.info(
                'Successfully authenticated on ' + MASTODON_INSTANCE_DOMAIN +
                ' as @' + mastodon_username +
                ', login information now stored in mastodon.secret file')
        except BaseException as e:
            logger.error('Error while logging into Mastodon: %s' % e)
            logger.error('Tootbot cannot continue, now shutting down')
            exit()
    else:
        try:
            mastodon = Mastodon(access_token='mastodon.secret',
                                api_base_url='https://' +
                                             MASTODON_INSTANCE_DOMAIN)
            # Make sure authentication is working
            username = mastodon.account_verify_credentials()['username']
            logger.info('Successfully authenticated on %s as @%s' % (MASTODON_INSTANCE_DOMAIN, username))
        except BaseException as e:
            logger.error('Error while logging into Mastodon: %s' % e)
            logger.error('Tootbot cannot continue, now shutting down')
            exit()
# Set the command line window title on Windows
if os.name == 'nt':
    try:
        if POST_TO_TWITTER and MASTODON_INSTANCE_DOMAIN:
            # Set title with both Twitter and Mastodon usernames
            # twitter_username = twitter.me().screen_name
            mastodon_username = mastodon.account_verify_credentials()['username']
            os.system('title ' + twitter_username + '@twitter.com and ' +
                      mastodon_username + '@' + MASTODON_INSTANCE_DOMAIN +
                      ' - Tootbot')
        elif POST_TO_TWITTER:
            # Set title with just Twitter username
            twitter_username = twitter.me().screen_name
            os.system('title ' + '@' + twitter_username + ' - Tootbot')
        elif MASTODON_INSTANCE_DOMAIN:
            # Set title with just Mastodon username
            mastodon_username = mastodon.account_verify_credentials()['username']
            os.system('title ' + mastodon_username + '@' +
                      MASTODON_INSTANCE_DOMAIN + ' - Tootbot')
    except:
        os.system('title Tootbot')

# Run the main script
NUM_NON_PROMO_MESSAGES = 0  # type: int
while True:
    # Make sure logging file and media directory exists
    if not os.path.exists(CACHE_CSV):
        with open(CACHE_CSV, 'w', newline='') as new_cache_file:
            default = ['Reddit post ID', 'Date and time', 'Post link']
            wr = csv.writer(new_cache_file)
            wr.writerow(default)
        logger.info('%s file not found, created a new one' % CACHE_CSV)
        new_cache_file.close()
    # Continue with script
    try:
        reddit_connection = setup_connection_reddit(SUBREDDIT_TO_MONITOR)
        reddit_posts = get_reddit_posts(reddit_connection)
        make_post(reddit_posts)
    except BaseException as e:
        logger.error('Error in main process: %s' % e)

    if RUN_ONCE_ONLY:
        logger.info('Exiting because RunOnceOnly is set to %s', RUN_ONCE_ONLY)
        sys.exit()

    logger.info('Sleeping for %s seconds' % DELAY_BETWEEN_TWEETS)
    time.sleep(DELAY_BETWEEN_TWEETS)
    logger.info('Restarting main process...')
