#!/usr/bin/env python3
#*-* coding: utf-8 *-*
import queue
from queue import Empty
import threading
import datetime
import html
import requests
import configparser
from tweepy.streaming import StreamListener
from tweepy import OAuthHandler
from tweepy import Stream
import telegram
import pprint
import json
import sqlite3
import random
import tempfile

config = configparser.ConfigParser()
config.read('/usr/local/share/twitegram/twitegram.conf')
follow_users = json.loads(config['twitter']['follow_users'])

LAST_UPDATE_ID = None

def dict_factory(cursor, row):
  d = {}
  for idx, col in enumerate(cursor.description):
    d[col[0]] = row[idx]
  return d

class TwitterStreamListener(StreamListener):
  def on_data(self, data):
    parsed = json.loads(data)
    pprint.pprint(parsed) 
    # Pass the data on to the bot
    #import pdb; pdb.set_trace()
    if 'text' in parsed and 'in_reply_to_screen_name' in parsed:
      # We'll ignore replies and @tweets
      if parsed['in_reply_to_screen_name'] is None and 'retweeted_status' not in parsed:
        print(parsed['text'])
        self.queue.put(parsed)
    return True

  def on_error(self, status):
    print(status)

def stream_reader(queue):
  listener = TwitterStreamListener()
  listener.queue = queue
  auth = OAuthHandler(config['twitter']['consumer_key'], config['twitter']['consumer_secret'])
  auth.set_access_token(config['twitter']['access_token'], config['twitter']['access_token_secret'])
  stream = Stream(auth, listener)

  # This blocks
  stream.filter(follow=follow_users)

def create_tables(db):
  db.execute("""CREATE TABLE IF NOT EXISTS subscribers(
      id integer primary key autoincrement,
      chat_id text,
      annoyance integer,
      join_announce boolean DEFAULT 0,
      announcement text);""")

def get_join_announcement(chat_id, db):
  db.execute("SELECT * FROM subscribers WHERE chat_id=:chat_id",
             {"chat_id" : chat_id })
  result = db.fetchone()
  if result is not None:
    # join_announce is set AND this is a group chat (negative chat_id)
    if result['join_announce'] and chat_id < 0:
      return result['announcement']
  return None

def set_join_announcement(chat_id, db, announcement):
  db.execute("UPDATE subscribers SET announcement=:announcement WHERE chat_id=:chat_id",
             {"announcement" : announcement, "chat_id" : chat_id })

def set_join_announcement_state(chat_id, db, state):
  db.execute("UPDATE subscribers SET join_announce=? WHERE chat_id=?",
             (state, chat_id))

def check_subscriber(chat_id, db):
  db.execute("SELECT * FROM subscribers WHERE chat_id=:chat_id", 
             {"chat_id" : chat_id })
  result = db.fetchone()
  if result is not None:
    return True
  return False
  
def add_subscription(chat_id, db):
  db.execute("INSERT INTO subscribers(chat_id, annoyance) VALUES (?, 0)", (chat_id,))

def increment_annoyance(chat_id, db):
  db.execute("SELECT annoyance FROM subscribers WHERE chat_id = ?", (chat_id,))
  result = db.fetchone()
  if result is not None and result['annoyance'] is not None:
    db.execute("UPDATE subscribers SET annoyance=? WHERE chat_id=?", (result['annoyance']+1, chat_id))
    return result['annoyance']
  else:
    db.execute("UPDATE subscribers SET annoyance=0 WHERE id=?", (chat_id,))
    return 0

def get_subscribers(db):
  db.execute("SELECT * FROM subscribers");
  r = []
  for row in db.fetchall():
    r.append(row['chat_id'])
  return r

def remove_subscriber(chat_id, db):
  db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

def get_witty_response():
  try:
    response = requests.get('http://quandyfactory.com/insult/json')
    return response.json()['insult']
  except requests.RequestException:
    return "Your father was a hamster and your mother smelt of elderberries"

def chuck_norris(firstName, lastName):
  try:
    response = requests.get('http://api.icndb.com/jokes/random', 
                   params={ 'firstName' : firstName, 'lastName' : lastName })
    result = response.json()
    if result['type'] == 'success':
      return html.unescape(result['value']['joke'])
  except requests.RequestException:
    return "Your lack of faith is disturbing.  (Command failed with an error)"
    

def get_events():
  try:
    response = requests.get('https://api.meetup.com/2/events?offset=0&format=json&limited_events=False&group_urlname=novafurs&photo-host=public&page=20&fields=&order=time&desc=false&status=upcoming&sig_id=9889908&sig=a65a4cd6d6c3491425a1e7a29554b42b1f78a5b4')
    events = response.json()['results']
    format_text = """{yes_rsvp_count} {group[who]} are going to _'{name}'_ on *{time_printable}*
Details: {event_url}\n
"""
    message = ''

    for event in events:
      if event['status'] == 'upcoming' and event['announced']:
        # Convert js dates (ms since epoch) to native datetimes:
        event['time_printable'] = datetime.datetime.fromtimestamp(int(event['time']/1000)).strftime("%A, %d %B at %H:%M")
        message += format_text.format(**event)
    if message != '':
      return message
    else:
      return "There are no events listed at this time."
  except requests.RequestException:
    return "Unable to fetch events at this time."  

def get_random_photo(bot, chat_id):
  try:
    response = requests.get("https://api.meetup.com/2/photos?offset=0&format=json&group_urlname=novafurs&photo-host=public&page=100&fields=&order=time&desc=True&sig_id=9889908&sig=9928c47f2e347dd2a9ccf9c3af4138fc4e185f5a")
    if not response.ok:
      bot.sendMessage(chat_id=chat_id, text='API call failed')
      return
      
    photos = response.json()['results']
    photo = random.choice(photos)
    bot.sendChatAction(chat_id=chat_id, action=telegram.ChatAction.UPLOAD_PHOTO)
    with tempfile.NamedTemporaryFile(suffix='.jpg') as photo_file:
      response = requests.get(photo['photo_link'], stream=True)
      if not response.ok:
        bot.sendMessage(chat_id=chat_id, text='Unable to fetch image')
        return
      
      for block in response.iter_content(1024):
        photo_file.write(block)
      
      caption = "Here's a photo uploaded by {0}:\n{1}".format(photo['member']['name'], photo['site_link'])
      bot.sendPhoto(chat_id=chat_id, photo=open(photo_file.name, 'rb'), caption=caption)

  except requests.RequestException:
    bot.sendMessage(chat_id=chat_id, text='API call failed. A team of highly trained foxes have been dispatched to fix the problem.')
    return

def interact(bot, db):
  global LAST_UPDATE_ID

  #Request updates after the last updated_id:
  for update in bot.getUpdates(offset=LAST_UPDATE_ID, timeout=10):
    chat_id = update.message.chat_id
    message = update.message.text

    # Add a subscription if needed:
    print("Incoming message: ({0}) '{1}'".format(chat_id, message))

    if message == '': #Probably a service message (join/part, etc)
      if update.message.new_chat_participant is not None:
        # Ignore user if they are a bot:
        joined_user = update.message.new_chat_participant.username
        if joined_user.lower().endswith('bot'): #XXX probably not the best way to check
          pass
        else:
          announce = get_join_announcement(chat_id, db)
          if announce is not None:
            bot.sendMessage(chat_id=chat_id, text=announce.format(**{'user' : joined_user}), parse_mode='Markdown')
    
    if message.startswith('/enable_join_message'):
      set_join_announcement_state(chat_id, db, True)
      bot.sendMessage(chat_id=chat_id, text='OK')
    if message.startswith('/disable_join_message'):
      set_join_announcement_state(chat_id, db, False) 
      bot.sendMessage(chat_id=chat_id, text='OK')
    if message.startswith('/set_join_message'):
      join_message = message[17:]
      set_join_announcement(chat_id, db, join_message)
      bot.sendMessage(chat_id=chat_id, text='OK')
    if message.startswith('/test_join_message'): 
      announce = get_join_announcement(chat_id, db)
      if announce is not None:
        bot.sendMessage(chat_id=chat_id, text=announce.format(**{'user' : 'TestUser'}), parse_mode='Markdown')
    if message.startswith('/photo'):
      get_random_photo(bot, chat_id)

    if message.startswith('/stop'):
      remove_subscriber(chat_id, db)
      bot.sendMessage(chat_id=chat_id, text="Channel unsubscribed.")
    elif '/events' in message:
      bot.sendMessage(chat_id=chat_id, text=get_events(), parse_mode='Markdown')
    elif message.lower().startswith('/norris'):
      parsed = message.split(' ')
      if len(parsed) >= 3:
        norris = chuck_norris(parsed[1], parsed[2])
      elif len(parsed) == 2:
        norris = chuck_norris(parsed[1], None)
      else:
        norris = chuck_norris(None, None)
      bot.sendMessage(chat_id=chat_id, text=norris)
    elif message == '/start':
      if check_subscriber(chat_id, db) == False:
        add_subscription(chat_id, db)
        bot.sendMessage(chat_id=chat_id,
          text="You are now subscribed to twitter event updates.")
      else:
        annoyance = increment_annoyance(chat_id, db)
        if annoyance >= 2:
          response = get_witty_response()
        else:
          response = "You are already subscribed. Use /stop to unsubscribe" 
        bot.sendMessage(chat_id=chat_id, text=response)

    # Update new update offset:
    LAST_UPDATE_ID = update.update_id + 1

def send_notifications(twitter_data, bot, db):
  # Get all subscriber message ID's and send them the tweet:
  subscribers = get_subscribers(db)
  for chat_id in subscribers:
    format_text = """*New tweet from @{username}:*
{text}

(https://twitter.com/{username}/status/{id})"""
    text = format_text.format(**{
        'username' : twitter_data['user']['screen_name'],
        'text' : twitter_data['text'],
        'id' : twitter_data['id'],
        'user_photo' : twitter_data['user']['profile_image_url_https']
      })
    bot.sendMessage(chat_id=chat_id, text=text, parse_mode='Markdown')

if __name__ == '__main__':
  conn = sqlite3.connect("/usr/local/share/twitegram/novafursbot.db", isolation_level=None)
  conn.row_factory = dict_factory
  db = conn.cursor()
  create_tables(db)

  queue = queue.Queue()
  #irc_queue = queue.Queue()

  global LAST_UPDATE_ID
  bot = telegram.Bot(config['telegram']['token'])
  
  try:
    LAST_UPDATE_ID = bot.getUpdates()[-1].update_id
  except IndexError:
    LAST_UPDATE_ID = None

  # Daemonize the twitter stream
  stream = threading.Thread(target=stream_reader, args=(queue,))
  stream.daemon = True
  stream.start()

  while True:
    try:
      twitter_data = queue.get(timeout=0.05)
      send_notifications(twitter_data, bot, db)
    except Empty:
      pass
    interact(bot, db)

