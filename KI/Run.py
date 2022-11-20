from calendar import weekday
from mylib.centroidtracker import CentroidTracker
from mylib.trackableobject import TrackableObject
from imutils.video import VideoStream
from imutils.video import FPS
from mylib.mailer import Mailer
from mylib import config, thread
import time
import schedule
import csv
import numpy as np
import argparse
import imutils
import time
import dlib
import cv2
import datetime
from itertools import zip_longest
#from database import DBManager
import json

t0 = time.time()
settings = {}
# Vairable zum Stoppen zum Stoppen der KI
isActive = True
positionLinie = 100


def run():
    print("started running")
    endzeit = read_config()["timeend"][weekday()]
    # construct the argument parse and parse the arguments
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--prototxt", required=False,
                    help="path to Caffe 'deploy' prototxt file")
    ap.add_argument("-m", "--model", required=True,
                    help="path to Caffe pre-trained model")
    ap.add_argument("-i", "--input", type=str,
                    help="path to optional input video file")
    ap.add_argument("-o", "--output", type=str,
                    help="path to optional output video file")
    # confidence default 0.4
    ap.add_argument("-c", "--confidence", type=float, default=0.4,
                    help="minimum probability to filter weak detections")
    ap.add_argument("-s", "--skip-frames", type=int, default=30,
                    help="# of skip frames between detections")
    args = vars(ap.parse_args())

    # initialize the list of class labels MobileNet SSD was trained to
    # detect
    CLASSES = ["background", "aeroplane", "bicycle", "bird", "boat",
               "bottle", "bus", "car", "cat", "chair", "cow", "diningtable",
               "dog", "horse", "motorbike", "person", "pottedplant", "sheep",
               "sofa", "train", "tvmonitor"]

    # load our serialized model from disk
    net = cv2.dnn.readNetFromCaffe(
        "KI/mobilenet_ssd/MobileNetSSD_deploy.prototxt", "KI/mobilenet_ssd/MobileNetSSD_deploy.caffemodel")

    # if a video path was not supplied, grab a reference to the ip camera
    if not args.get("input", False):
        print("[INFO] Starting the live stream..")
        vs = VideoStream(config.url).start()
        time.sleep(2.0)

    # otherwise, grab a reference to the video file
    else:
        print("[INFO] Starting the video..")
        vs = cv2.VideoCapture(args["input"])

    # initialize the video writer (we'll instantiate later if need be)
    writer = None

    # initialize the frame dimensions (we'll set them as soon as we read
    # the first frame from the video)
    W = None
    H = None

    # instantiate our centroid tracker, then initialize a list to store
    # each of our dlib correlation trackers, followed by a dictionary to
    # map each unique object ID to a TrackableObject
    ct = CentroidTracker(maxDisappeared=40, maxDistance=50)
    trackers = []
    trackableObjects = {}

    # initialize the total number of frames processed thus far, along
    # with the total number of objects that have moved either up or down
    totalFrames = 0
    totalDown = 0
    totalUp = 0
    # total of people currently inside the building
    totalIn = 0
    # Setting up database manager
    dbmanager = DBManager()
    dbmanager.addToDB(totalIn)

    x = []
    empty = []
    empty1 = []

    # start the frames per second throughput estimator
    fps = FPS().start()

    if config.Thread:
        vs = thread.ThreadingClass(config.url)

    # loop over frames from the video stream
    # while isActive:
    while datetime.time.hour != endzeit[:1] and datetime.time.minute != endzeit[2:3]:
        # Zeit zum Beenden der KI laden
        jsonData = read_config()

        # grab the next frame and handle if we are reading from either
        # VideoCapture or VideoStream
        frame = vs.read()
        frame = frame[1] if args.get("input", False) else frame

        # if we are viewing a video and we did not grab a frame then we
        # have reached the end of the video
        if args["input"] is not None and frame is None:
            break

        # resize the frame to have a maximum width of 500 pixels (the
        # less data we have, the faster we can process it), then convert
        # the frame from BGR to RGB for dlib
        frame = imutils.resize(frame, width=500)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # if the frame dimensions are empty, set them
        if W is None or H is None:
            (H, W) = frame.shape[:2]

        # if we are supposed to be writing a video to disk, initialize
        # the writer
        if args["output"] is not None and writer is None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(args["output"], fourcc, 30,
                                     (W, H), True)

        # initialize the current status along with our list of bounding
        # box rectangles returned by either (1) our object detector or
        # (2) the correlation trackers
        status = "Waiting"
        rects = []

        # check to see if we should run a more computationally expensive
        # object detection method to aid our tracker
        if totalFrames % args["skip_frames"] == 0:
            # set the status and initialize our new set of object trackers
            status = "Detecting"
            trackers = []

            # convert the frame to a blob and pass the blob through the
            # network and obtain the detections
            blob = cv2.dnn.blobFromImage(frame, 0.007843, (W, H), 127.5)
            net.setInput(blob)
            detections = net.forward()

            # loop over the detections
            for i in np.arange(0, detections.shape[2]):
                # extract the confidence (i.e., probability) associated
                # with the prediction
                confidence = detections[0, 0, i, 2]

                # filter out weak detections by requiring a minimum
                # confidence
                if confidence > args["confidence"]:
                    # extract the index of the class label from the
                    # detections list
                    idx = int(detections[0, 0, i, 1])

                    # if the class label is not a person, ignore it
                    if CLASSES[idx] != "person":
                        continue

                    # compute the (x, y)-coordinates of the bounding box
                    # for the object
                    box = detections[0, 0, i, 3:7] * np.array([W, H, W, H])
                    (startX, startY, endX, endY) = box.astype("int")

                    # construct a dlib rectangle object from the bounding
                    # box coordinates and then start the dlib correlation
                    # tracker
                    tracker = dlib.correlation_tracker()
                    rect = dlib.rectangle(startX, startY, endX, endY)
                    tracker.start_track(rgb, rect)

                    # add the tracker to our list of trackers so we can
                    # utilize it during skip frames
                    trackers.append(tracker)

        # otherwise, we should utilize our object *trackers* rather than
        # object *detectors* to obtain a higher frame processing throughput
        else:
            # loop over the trackers
            for tracker in trackers:
                # set the status of our system to be 'tracking' rather
                # than 'waiting' or 'detecting'
                status = "Tracking"

                # update the tracker and grab the updated position
                tracker.update(rgb)
                pos = tracker.get_position()

                # unpack the position object
                startX = int(pos.left())
                startY = int(pos.top())
                endX = int(pos.right())
                endY = int(pos.bottom())

                # add the bounding box coordinates to the rectangles list
                rects.append((startX, startY, endX, endY))

        # draw a horizontal line in the center of the frame -- once an
        # object crosses this line we will determine whether they were
        # moving 'up' or 'down'
        cv2.line(frame, (0, H // 2), (W, H // 2), (0, 0, 0), 3)
        # cv2.putText(frame, "-Prediction border - Entrance-", (10, H - ((i * 20) + 200)),
        # cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)

        # use the centroid tracker to associate the (1) old object
        # centroids with (2) the newly computed object centroids
        objects = ct.update(rects)

        # loop over the tracked objects
        for (objectID, centroid) in objects.items():
            # check to see if a trackable object exists for the current
            # object ID
            to = trackableObjects.get(objectID, None)

            # if there is no existing trackable object, create one
            if to is None:
                to = TrackableObject(objectID, centroid)

            # otherwise, there is a trackable object so we can utilize it
            # to determine direction
            else:
                # the difference between the y-coordinate of the *current*
                # centroid and the mean of *previous* centroids will tell
                # us in which direction the object is moving (negative for
                # 'up' and positive for 'down')
                y = [c[1] for c in to.centroids]
                direction = centroid[1] - np.mean(y)
                to.centroids.append(centroid)

                # check to see if the object has been counted or not
                if not to.counted:
                    # if the direction is negative (indicating the object
                    # is moving up) AND the centroid is above the center
                    # line, count the object
                    # if direction < 0 and centroid[1] < H // 2:
                    if direction < 0 and centroid[1] < positionLinie:
                        totalUp += 1
                        totalIn += 1
                        dbmanager.addToDB(totalIn)
                        empty.append(totalUp)
                        to.counted = True

                    # if the direction is positive (indicating the object
                    # is moving down) AND the centroid is below the
                    # center line, count the object
                    # elif direction > 0 and centroid[1] > H // 2:
                    elif direction > 0 and centroid[1] > positionLinie:
                        totalDown += 1
                        totalIn -= 1
                        dbmanager.addToDB(totalIn)
                        empty1.append(totalDown)
                        # print(empty1[-1])
                        # if the people limit exceeds over threshold, send an email alert
                        if sum(x) >= config.Threshold:
                            cv2.putText(frame, "-ALERT: People limit exceeded-", (10, frame.shape[0] - 80),
                                        cv2.FONT_HERSHEY_COMPLEX, 0.5, (0, 0, 255), 2)
                            if config.ALERT:
                                print("[INFO] Sending email alert..")
                                Mailer().send(config.MAIL)
                                print("[INFO] Alert sent")

                        to.counted = True

                    x = []
                    # compute the sum of total people inside
                    x.append(len(empty1)-len(empty))
                    #print("Total people inside:", x)

            # store the trackable object in our dictionary
            trackableObjects[objectID] = to

            # draw both the ID of the object and the centroid of the
            # object on the output frame
            text = "ID {}".format(objectID)
            # cv2.putText(frame, text, (centroid[0] - 10, centroid[1] - 10),
            # cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
            #cv2.circle(frame, (centroid[0], centroid[1]), 4, (255, 255, 255), -1)

        # construct a tuple of information we will be displaying on the
        info = [
            ("Exit", totalUp),
            ("Enter", totalDown),
            ("Status", status),
        ]

        info2 = [
            ("Total people inside", x),
        ]

        # Display the output
        for (i, (k, v)) in enumerate(info):
            text = "{}: {}".format(k, v)
            cv2.putText(frame, text, (10, H - ((i * 20) + 20)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

        for (i, (k, v)) in enumerate(info2):
            text = "{}: {}".format(k, v)
            cv2.putText(frame, text, (265, H - ((i * 20) + 60)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        # Initiate a simple log to save data at end of the day
        if config.Log:
            datetimee = [datetime.datetime.now()]
            d = [datetimee, empty1, empty, x]
            export_data = zip_longest(*d, fillvalue='')

            with open('Log.csv', 'w', newline='') as myfile:
                wr = csv.writer(myfile, quoting=csv.QUOTE_ALL)
                wr.writerow(("End Time", "In", "Out", "Total Inside"))
                wr.writerows(export_data)

        # check to see if we should write the frame to disk
        if writer is not None:
            writer.write(frame)

        # show the output frame
        #cv2.imshow("Real-Time Monitoring/Analysis Window", frame)
        #key = cv2.waitKey(1) & 0xFF

        # if the `q` key was pressed, break from the loop
        if key == ord("q"):
            break

        # increment the total number of frames processed thus far and
        # then update the FPS counter
        totalFrames += 1
        fps.update()

        if config.Timer:
            # Automatic timer to stop the live stream. Set to 8 hours (28800s).
            t1 = time.time()
            num_seconds = (t1-t0)
            if num_seconds > 28800:
                break

    # stop the timer and display FPS information
    fps.stop()
    print("[INFO] elapsed time: {:.2f}".format(fps.elapsed()))
    print("[INFO] approx. FPS: {:.2f}".format(fps.fps()))

    # # if we are not using a video file, stop the camera video stream
    # if not args.get("input", False):
    # 	vs.stop()
    #
    # # otherwise, release the video file pointer
    # else:
    # 	vs.release()

    # issue 15
    if config.Thread:
        vs.release()

    # close any open windows
    cv2.destroyAllWindows()
    stop()


def read_config():
    try:
        with open('Settings.json') as file:
            data = json.load(file)
            settings = json.loads(data)
            return settings
    except:
        print("Configfile not found")
        return "LoadingFailed"


def stop():
    laufvariable = 0
    global positionLinie
    positionLinie = read_config()["pos"]
    while laufvariable < 7:
        startzeit = read_config()["timestart"][weekday()+laufvariable % 7]
        if startzeit == "9999":
            laufvariable += 1

    while datetime.time.hour != startzeit[:1] and datetime.time.minute != startzeit[2:3]:
        pass

    run()


stop()
# learn more about different schedules here: https://pypi.org/project/schedule/
"""if config.Scheduler:
	settings = update_config()
	starttime = settings["timestart"][weekday()]
	updateSettings = True
	##Runs for every 1 second
	#schedule.every(1).seconds.do(run)
	##Runs at every day (09:00 am). You can change it.
	schedule.every().day.at("09:00").do(run)

	while 1:
		if datetime.time.hour == "01" and datetime.time.minute == "00" and updateSettings:
			settings = update_config()
			updateSettings = False

			if settings == "LoadingFailed":
				print("Error: Unable to load config file")
			else:
				starttime = str(settings["timestart"][weekday()])
				if starttime=="9999":
					print("KI started heute nicht")
					continue
				else:
					
			schedule.every().day.at(starttime).do(run)
		elif datetime.time.hour != "01" and datetime.time.minute != "00" and not updateSettings:
			updateSettings = True
		
		schedule.run_pending()

else:
	run()"""
