#!/usr/bin/env python

import client
import praw
import pyimgur

import sys
import math
import time
import argparse
import subprocess
from string import Template

import urllib2
import zipfile
import tempfile
from xml.dom.minidom import parseString

import credentials

class AstroBot(object):
    def __init__(self):
        # Astrometry API
        self.api = client.client.Client()
        self.api.login(credentials.ASTROMETRY_ID)

        # Reddit API
        self.praw = praw.Reddit(user_agent = credentials.USER_AGENT)
        self.praw.login(credentials.REDDIT_USER, credentials.REDDIT_PASSWORD)

        # Imgur API
        self.imgur = pyimgur.Imgur(credentials.IMGUR_CLIENT_ID, client_secret=credentials.IMGUR_CLIENT_SECRET)
        print("Get PIN on " + credentials.IMGUR_AUTH_URL)
        authorized = False
        while (not authorized):
            sys.stdout.flush()
            pin = raw_input("Enter PIN: ")
            try:
                self.imgur.exchange_pin(pin)
                authorized = True
            except:
                print("Wrong PIN or other error. Authorize again.")

        self._rightAscension = 0
        self._declination = 0
        self._range = 0
        self._tags = []
        self._annotated_image = ""

    def process(self, url, image_url=None):
        thread = self.praw.get_submission(url=url)
        self.author = thread.author
        if (image_url is not None):
            self.image = image_url
        else:
            self.image = thread.url

        job_id = self._upload(self.image)
        success = self._wait_for_job(job_id)
        if (success):
            self.processSolved(url, job_id)
        else:
            print 'Failed to solve the picture.'

    def processSolved(self, url, job_id):
        thread = self.praw.get_submission(url=url)
        self.author = thread.author
        self.job_id = job_id

        self._tags = self.api.send_request('jobs/%s/tags' % self.job_id, {})["tags"]
        # if there are too many tags, filter out stars
        if (len(self._tags) > 8):
            self._tags = filter(lambda x: x.find("star") == -1, self._tags)

        self._parse_kml("http://nova.astrometry.net/kml_file/" + self.job_id)
        self._annotated_image = self._upload_annotated()

        # Post to reddit
        thread.add_comment(self._create_comment())
        thread.upvote() # can I do that?
        thread.save()

    def _upload(self, image_url):
        """Uploads the image on given url to Astrometry and waits for job id."""

        kwargs = dict(
                allow_commercial_use="n",
                allow_modifications="n",
                publicly_visible="y")

        result = self.api.url_upload(image_url, **kwargs)

        stat = result['status']
        if stat != 'success':
            print 'Upload failed: status', stat
            sys.exit(-1)

        sub_id = result['subid']
        job_id = None
        while True:
            subStat = self.api.sub_status(sub_id, justdict=True)
            jobs = subStat.get('jobs',[])
            if len(jobs):
                for j in jobs:
                    if j is not None:
                        break
                if j is not None:
                    job_id = j
                    break
            time.sleep(5)
        return str(job_id)

    # TODO: rewrite to python
    def _upload_annotated(self):
        """Get annotated image from astrometry, put label on it and upload to imgur"""

        subprocess.check_call(["./annotate.sh", self.job_id, str(self.author)])

        self.imgur.refresh_access_token()
        try:
            uploaded_image = self.imgur.upload_image(path=self.job_id + ".png", album=credentials.ALBUM_ID)
            return uploaded_image.link
        except:
            print("Imgur error")
            sys.exit(-1)

    def _wait_for_job(self, job_id):
        """Wait for the result of job."""

        while True:
            stat = self.api.job_status(job_id, justdict=True)
            if stat.get('status','') in ['success']:
                return True
            if stat.get('status','') in ['failure']:
                return False
            time.sleep(5)

    def _parse_kml(self, path):
        """Download KML file of solved job and parse it."""

        file = urllib2.urlopen(path)
        pkdata = file.read()
        tmp = tempfile.NamedTemporaryFile()
        tmp.write(pkdata)
        tmp.flush()

        zf = zipfile.ZipFile(tmp.name)
        data = zf.read("doc.kml")

        zf.close()
        file.close()

        #parse the xml you got from the file
        dom = parseString(data)

        longitude = dom.getElementsByTagName('longitude')[0].firstChild.nodeValue
        self._rightAscension = (float(longitude) + 180)/15.0
        self._declination = float(dom.getElementsByTagName('latitude')[0].firstChild.nodeValue)
        self._range = float(dom.getElementsByTagName('range')[0].firstChild.nodeValue)

    def _hours_to_real(self, hours, minutes, seconds):
        return hours + minutes / 60.0 + seconds / 3600.0

    def _real_to_hours(self, real):
        hours = int(real)
        n = real - hours
        if (n < 0):
            n = -n

        minutes = int(math.floor(n * 60))
        n = n - minutes / 60.0
        seconds = n * 3600

        return (hours, minutes, seconds)

    def _wikisky_link(self):
        link = "http://server4.wikisky.org/v2"

        link += "?ra=" + str(self._rightAscension)
        link += "&de=" + str(self._declination)

        zoom = 18 - round(math.log(self._range / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        link += "&show_grid=1&show_constellation_lines=1"
        link += "&show_constellation_boundaries=1&show_const_names=1"
        link += "&show_galaxies=1&img_source=SKY-MAP"

        return link

    def _googlesky_link(self):
        link = "http://www.google.com/sky/"

        link += "#latitude=" + str(self._declination)
        link += "&longitude=" + str(self._rightAscension*15 - 180)

        zoom = 20 - round(math.log(self._range / 90.0) / math.log(2))
        link += "&zoom=" + str(int(zoom))

        return link

    def _create_comment(self):
        """Construct the comment for reddit."""

        data = dict()
        data["coordinates"] = "> [Coordinates](http://en.wikipedia.org/wiki/Celestial_coordinate_system)"

        (hh, mm, ss) = self._real_to_hours(self._rightAscension)
        data["hh"] = '%d^h' % hh
        data["mm"] = '%d^m' % mm
        data["ss"] = '%.2f^s' % ss

        (hh, mm, ss) = self._real_to_hours(self._declination)
        data["h2"] = '%d^o' % hh
        data["m2"] = '%d\'' % mm
        data["s2"] = '%.2f"' % ss

        if (self._annotated_image is not None):
            imageLinks = "> Annotated image: [$url]($url)\n\n"
            data["image"] = Template(imageLinks).safe_substitute(
                    {"url":self._annotated_image})

        if (len(self._tags) > 0):
            data["tags"] = "> Tags^1: *" + ", ".join(self._tags) + "*\n\n"

        data["google"] = "[Google sky](" + self._googlesky_link() + ")"
        data["wikisky"] = "[WIKISKY.ORG](" + self._wikisky_link() + ")"
        data["links"] = Template("> Links: $google | $wikisky\n\n").safe_substitute(data)

        message =  "This is an automatically generated comment.\n\n"
        message += "$coordinates: $hh $mm $ss , $h2 $m2 $s2\n\n"
        message += "$image"
        message += "$tags"
        message += "$links"
        message += "*****\n\n"
        message += "*Powered by [Astrometry.net]("
        message += "http://nova.astrometry.net/users/540)* | "
        message += "[*Feedback*]("
        message += "http://www.reddit.com/message/compose?to=%23astro-bot)\n"
        message += " | [FAQ](http://www.reddit.com/r/faqs/comments/1ninoq/uastrobot_faq/) "
        message += " | &nbsp;^1 ) *Tags may overlap.*\n"

        return Template(message).safe_substitute(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='astrobot')
    exclusive = parser.add_mutually_exclusive_group()
    parser.add_argument(
            "-u", "--url",
            help="URL of reddit thread")
    exclusive.add_argument(
            "-i", "--image",
            help="URL of image (optional)")
    exclusive.add_argument(
            "-j", "--jobid",
            help="Process already solved job")
    args = parser.parse_args()

    bot = AstroBot()
    if (args.url is not None):
        if (args.jobid is not None):
            bot.processSolved(args.url, args.jobid)
        else:
            bot.process(args.url, args.image)
    else:
        while (True):
            try:
                line = raw_input("Enter reddit thread url: ")
                bot.process(line)
                print("Done.")
            except KeyboardInterrupt:
                break
        print("Good bye!")
