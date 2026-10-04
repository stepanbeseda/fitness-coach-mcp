"""Garmin response samples.

Field names follow python-garminconnect 0.3.17's typed response models
(garminconnect/typed.py) and payloads observed by other Garmin MCP projects;
values are made up.
"""

DAY = "2026-09-30"

TRAINING_READINESS = [
    {
        "userProfilePK": 1,
        "calendarDate": DAY,
        "timestamp": "2026-09-30T12:05:00.0",
        "timestampLocal": "2026-09-30T14:05:00.0",
        "deviceId": 3,
        "level": "MODERATE",
        "feedbackLong": "RECOVERED_AND_READY_FOR_MODERATE",
        "feedbackShort": "READY_FOR_MODERATE",
        "score": 58,
        "sleepScore": 71,
        "sleepScoreFactorPercent": 68,
        "sleepScoreFactorFeedback": "MODERATE",
        "sleepHistoryFactorPercent": 74,
        "sleepHistoryFactorFeedback": "GOOD",
        "recoveryTime": 540,
        "recoveryTimeFactorPercent": 55,
        "recoveryTimeFactorFeedback": "MODERATE",
        "recoveryTimeChangePhrase": "DECREASING",
        "acwrFactorPercent": 80,
        "acwrFactorFeedback": "GOOD",
        "acuteLoad": 612,
        "hrvFactorPercent": 70,
        "hrvFactorFeedback": "GOOD",
        "hrvWeeklyAverage": 52,
        "stressHistoryFactorPercent": 64,
        "stressHistoryFactorFeedback": "MODERATE",
        "inputContext": "UPDATE_REALTIME_VARIABLES",
    },
    {
        "calendarDate": DAY,
        "timestamp": "2026-09-30T04:40:00.0",
        "timestampLocal": "2026-09-30T06:40:00.0",
        "level": "HIGH",
        "feedbackShort": "READY_FOR_HARD",
        "score": 76,
        "sleepScore": 71,
        "recoveryTime": 600,
        "inputContext": "AFTER_WAKEUP_RESET",
    },
]

HRV = {
    "userProfilePK": 1,
    "hrvSummary": {
        "calendarDate": DAY,
        "weeklyAvg": 52,
        "lastNightAvg": 48,
        "lastNight5MinHigh": 71,
        "baseline": {"lowUpper": 44, "balancedLow": 47, "balancedUpper": 60, "markerValue": 0.4},
        "status": "BALANCED",
        "feedbackPhrase": "HRV_BALANCED_1",
    },
    "hrvReadings": [{"hrvValue": 50, "readingTimeGMT": "2026-09-29T22:00:00.0"}] * 50,
}

HRV_RANGE = {
    "hrvSummaries": [
        {"calendarDate": f"2026-09-{d:02d}", "weeklyAvg": 50 + d % 3, "lastNightAvg": 45 + d % 7,
         "baseline": {"balancedLow": 47, "balancedUpper": 60}, "status": "BALANCED"}
        for d in range(17, 31)
    ]
}

SLEEP = {
    "dailySleepDTO": {
        "id": 1,
        "calendarDate": DAY,
        "sleepTimeSeconds": 25380,
        "napTimeSeconds": 0,
        "sleepStartTimestampGMT": 1759178400000,
        "sleepEndTimestampGMT": 1759204200000,
        "sleepStartTimestampLocal": 1759185600000,  # 2025-09-29T22:40 local wall time
        "sleepEndTimestampLocal": 1759211400000,
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 13800,
        "remSleepSeconds": 5580,
        "awakeSleepSeconds": 600,
        "awakeCount": 2,
        "avgSleepStress": 14.0,
        "averageRespirationValue": 13.6,
        "averageSpO2Value": 95.0,
        "lowestSpO2Value": 89,
        "sleepScoreFeedback": "POSITIVE_DEEP",
        "sleepScoreInsight": "NONE",
        "sleepScores": {
            "overall": {"value": 79, "qualifierKey": "FAIR"},
            "totalDuration": {"qualifierKey": "FAIR"},
            "stress": {"qualifierKey": "GOOD"},
            "remPercentage": {"value": 22, "qualifierKey": "EXCELLENT"},
            "deepPercentage": {"value": 21, "qualifierKey": "GOOD"},
            "restlessness": {"qualifierKey": "GOOD"},
        },
    },
    "avgOvernightHrv": 48.0,
    "restingHeartRate": 49,
    "restlessMomentsCount": 31,
    "bodyBatteryChange": 58,
    "sleepMovement": [{"startGMT": "x", "activityLevel": 1.0}] * 400,
}

USER_SUMMARY = {
    "calendarDate": DAY,
    "totalSteps": 8123,
    "dailyStepGoal": 9000,
    "totalDistanceMeters": 6450,
    "totalKilocalories": 2410.0,
    "activeKilocalories": 610.0,
    "restingHeartRate": 49,
    "lastSevenDaysAvgRestingHeartRate": 50,
    "minHeartRate": 45,
    "maxHeartRate": 151,
    "averageStressLevel": 27,
    "maxStressLevel": 91,
    "restStressDuration": 25200,
    "lowStressDuration": 14400,
    "mediumStressDuration": 3600,
    "highStressDuration": 900,
    "moderateIntensityMinutes": 22,
    "vigorousIntensityMinutes": 31,
    "intensityMinutesGoal": 150,
    "bodyBatteryChargedValue": 61,
    "bodyBatteryDrainedValue": 48,
    "bodyBatteryHighestValue": 88,
    "bodyBatteryLowestValue": 27,
    "bodyBatteryMostRecentValue": 40,
    "bodyBatteryAtWakeTime": 85,
    "floorsAscended": 7.93,
    "sleepingSeconds": 25380,
    "averageSpo2": 95,
    "avgWakingRespirationValue": 15.0,
    "privacyProtected": False,
}

TRAINING_STATUS = {
    "userId": 1,
    "mostRecentVO2Max": {
        "generic": {"calendarDate": "2026-09-28", "vo2MaxPreciseValue": 53.4, "vo2MaxValue": 53.0},
        "cycling": {"calendarDate": "2026-09-20", "vo2MaxPreciseValue": 55.1, "vo2MaxValue": 55.0},
    },
    "mostRecentTrainingLoadBalance": {
        "metricsTrainingLoadBalanceDTOMap": {
            "3": {
                "calendarDate": DAY,
                "monthlyLoadAerobicLow": 412.3,
                "monthlyLoadAerobicHigh": 380.7,
                "monthlyLoadAnaerobic": 95.2,
                "monthlyLoadAerobicLowTargetMin": 300,
                "monthlyLoadAerobicLowTargetMax": 600,
                "monthlyLoadAerobicHighTargetMin": 250,
                "monthlyLoadAerobicHighTargetMax": 500,
                "monthlyLoadAnaerobicTargetMin": 50,
                "monthlyLoadAnaerobicTargetMax": 200,
                "trainingBalanceFeedbackPhrase": "BALANCED",
                "primaryTrainingDevice": True,
            }
        }
    },
    "mostRecentTrainingStatus": {
        "latestTrainingStatusData": {
            "9": {"calendarDate": "2026-09-01", "trainingStatus": 4, "primaryTrainingDevice": False},
            "3": {
                "calendarDate": DAY,
                "sinceDate": "2026-09-24",
                "trainingStatus": 7,
                "trainingStatusFeedbackPhrase": "PRODUCTIVE_6",
                "trainingPaused": False,
                "fitnessTrend": 2,
                "acuteTrainingLoadDTO": {
                    "acwrPercent": 52,
                    "acwrStatus": "OPTIMAL",
                    "dailyTrainingLoadAcute": 612,
                    "dailyTrainingLoadChronic": 540,
                    "maxTrainingLoadChronic": 810.0,
                    "minTrainingLoadChronic": 432.0,
                    "dailyAcuteChronicWorkloadRatio": 1.13,
                },
                "primaryTrainingDevice": True,
            },
        }
    },
}

BODY_BATTERY = [
    {
        "date": "2026-09-29",
        "charged": 55,
        "drained": 60,
        "bodyBatteryValueDescriptorDTOList": [
            {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
            {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
        ],
        "bodyBatteryValuesArray": [[1759100000000, 70], [1759140000000, 25], [1759120000000, 50]],
    },
    {
        "date": DAY,
        "charged": 61,
        "drained": 48,
        "bodyBatteryValuesArray": [[1759186000000, 27], [1759210000000, 88], [1759230000000, 40]],
    },
]

ACTIVITIES = [
    {
        "activityId": 20001,
        "activityName": "Praha Běh",
        "startTimeLocal": "2026-09-29 18:02:11",
        "activityType": {"typeId": 1, "typeKey": "running"},
        "distance": 10020.0,
        "duration": 3005.4,
        "movingDuration": 2990.0,
        "elevationGain": 64.0,
        "averageSpeed": 3.334,
        "averageHR": 151.0,
        "maxHR": 176.0,
        "calories": 702.0,
        "aerobicTrainingEffect": 3.6,
        "anaerobicTrainingEffect": 1.2,
        "activityTrainingLoad": 142.7,
        "trainingEffectLabel": "TEMPO",
        "averageRunningCadenceInStepsPerMinute": 172.4,
    },
    {
        "activityId": 20002,
        "activityName": "Gravel",
        "startTimeLocal": "2026-09-27 09:15:00",
        "activityType": {"typeId": 2, "typeKey": "gravel_cycling"},
        "distance": 61500.0,
        "duration": 8100.0,
        "averageSpeed": 7.6,
        "averageHR": 132.0,
        "avgPower": 188.0,
        "normPower": 205.0,
        "activityTrainingLoad": 160.0,
    },
    {
        "activityId": 20003,
        "activityName": "Síla",
        "startTimeLocal": "2026-09-28 07:00:00",
        "activityType": {"typeId": 13, "typeKey": "strength_training"},
        "distance": 0.0,
        "duration": 3300.0,
        "averageHR": 101.0,
        "totalSets": 18,
        "totalReps": 164,
    },
]

ACTIVITY = {
    "activityId": 20001,
    "activityName": "Praha Běh",
    "description": "Tempo 3x10 min",
    "activityTypeDTO": {"typeId": 1, "typeKey": "running"},
    "eventTypeDTO": {"typeKey": "training"},
    "summaryDTO": {
        "startTimeLocal": "2026-09-29T18:02:11.0",
        "distance": 10020.0,
        "duration": 3005.4,
        "movingDuration": 2990.0,
        "elevationGain": 64.0,
        "elevationLoss": 61.0,
        "averageSpeed": 3.334,
        "maxSpeed": 4.9,
        "averageHR": 151.0,
        "maxHR": 176.0,
        "averageRunCadence": 172.4,
        "trainingEffect": 3.6,
        "anaerobicTrainingEffect": 1.2,
        "activityTrainingLoad": 142.7,
        "calories": 702.0,
    },
}

SPLITS = {
    "activityId": 20001,
    "lapDTOs": [
        {"lapIndex": i, "distance": 1000.0, "duration": 300.0 - i, "averageSpeed": 1000 / (300.0 - i),
         "averageHR": 140 + i, "maxHR": 150 + i, "averageRunCadence": 170.0, "intensityType": "ACTIVE",
         "wktStepIndex": 1}
        for i in range(1, 11)
    ],
}

HR_ZONES = [
    {"zoneNumber": 2, "secsInZone": 900.0, "zoneLowBoundary": 120},
    {"zoneNumber": 1, "secsInZone": 300.0, "zoneLowBoundary": 100},
    {"zoneNumber": 3, "secsInZone": 1500.0, "zoneLowBoundary": 140},
    {"zoneNumber": 4, "secsInZone": 300.0, "zoneLowBoundary": 158},
]

WORKOUTS = [
    {
        "workoutId": 9001,
        "workoutName": "Intervaly 5x3",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "estimatedDurationInSecs": 3000,
        "createdDate": "2026-09-01T10:00:00.0",
        "updatedDate": "2026-09-20T10:00:00.0",
    }
]

WORKOUT_DETAIL = {
    "workoutId": 9001,
    "workoutName": "Intervaly 5x3",
    "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    "estimatedDurationInSecs": 3000,
    "workoutSegments": [
        {
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSteps": [
                {"type": "ExecutableStepDTO", "stepOrder": 1, "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                 "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"}, "endConditionValue": 900.0,
                 "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}, "zoneNumber": 2},
                {"type": "RepeatGroupDTO", "stepOrder": 2, "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                 "numberOfIterations": 5,
                 "workoutSteps": [
                     {"type": "ExecutableStepDTO", "stepOrder": 3,
                      "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                      "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
                      "endConditionValue": 1000.0,
                      "targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"},
                      "targetValueOne": 3.2258, "targetValueTwo": 3.3333},
                     {"type": "ExecutableStepDTO", "stepOrder": 4,
                      "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                      "endCondition": {"conditionTypeId": 1, "conditionTypeKey": "lap.button"},
                      "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}},
                 ]},
            ],
        }
    ],
}

SCHEDULES_GRAPHQL = {
    "data": {
        "workoutScheduleSummariesScalar": [
            {"scheduledWorkoutId": 777, "scheduleDate": "2026-10-02", "workoutId": 9001,
             "workoutName": "Intervaly 5x3", "workoutType": "running", "associatedActivityId": None,
             "estimatedDurationInSecs": 3000},
            {"scheduledWorkoutId": 778, "scheduleDate": "2026-09-30", "workoutId": 9002,
             "workoutName": "Z2 kolo", "workoutType": "cycling", "associatedActivityId": 20009},
        ]
    }
}

CALENDAR_MONTH = {
    "calendarItems": [
        {"id": 555, "itemType": "workout", "date": "2026-10-03", "workoutId": 9001, "title": "Intervaly 5x3",
         "sportTypeKey": "running"},
        {"id": 556, "itemType": "activity", "date": "2026-10-03", "title": "Run"},
        {"id": 557, "itemType": "workout", "date": "2026-11-15", "workoutId": 9003, "title": "Far away"},
    ]
}

# ---------------------------------------------------------------- performance (PR 01)

RACE_PREDICTIONS = {"userId": 1, "calendarDate": DAY, "time5K": 1290, "time10K": 2700,
                    "timeHalfMarathon": 5970, "timeMarathon": 12310}

LACTATE_THRESHOLD = {  # already merged by garminconnect.get_lactate_threshold(latest=True)
    "speed_and_heart_rate": {"userProfilePK": 1, "version": 1, "calendarDate": "2026-09-12T00:00:00.0",
                             "sequence": 1, "speed": 0.3636, "heartRate": 172, "heartRateCycling": None},
    "power": {"userProfilePk": 1, "calendarDate": "2026-09-12", "sport": "RUNNING",
              "functionalThresholdPower": 345, "weight": 72.0, "powerToWeight": 4.79},
}

HEART_RATE_ZONES = [
    {"sport": "DEFAULT", "trainingMethod": "LACTATE_THRESHOLD", "restingHeartRateUsed": 48,
     "lactateThresholdHeartRateUsed": 172, "maxHeartRateUsed": 191, "zone1Floor": 103, "zone2Floor": 138,
     "zone3Floor": 155, "zone4Floor": 163, "zone5Floor": 172, "changeState": "UNCHANGED"},
    {"sport": "RUNNING", "trainingMethod": "LACTATE_THRESHOLD", "restingHeartRateUsed": 48,
     "lactateThresholdHeartRateUsed": 172, "maxHeartRateUsed": 191, "zone1Floor": 105, "zone2Floor": 140,
     "zone3Floor": 156, "zone4Floor": 164, "zone5Floor": 173, "changeState": "UNCHANGED"},
    {"sport": "CYCLING", "trainingMethod": "PERCENT_MAX_HR", "restingHeartRateUsed": 48,
     "lactateThresholdHeartRateUsed": None, "maxHeartRateUsed": 185, "zone1Floor": 93, "zone2Floor": 111,
     "zone3Floor": 130, "zone4Floor": 148, "zone5Floor": 167, "changeState": "UNCHANGED"},
    {"sport": "SWIMMING", "trainingMethod": "PERCENT_MAX_HR", "maxHeartRateUsed": 180},
]

POWER_ZONES = [
    {"sport": "CYCLING", "functionalThresholdPower": 250, "zone1Floor": 0, "zone2Floor": 138, "zone3Floor": 188,
     "zone4Floor": 225, "zone5Floor": 263, "zone6Floor": 300, "zone7Floor": 375},
    {"sport": "RUNNING", "functionalThresholdPower": 345, "zone1Floor": 0, "zone2Floor": 276, "zone3Floor": 311,
     "zone4Floor": 345, "zone5Floor": 380},
]

ENDURANCE_SCORE = {
    "userProfilePK": 1, "startDate": "2026-07-09", "endDate": DAY, "avg": 7150, "max": 7300,
    "groupMap": {"2026-09-28": {"groupAverage": 7240, "groupMax": 7260},
                 "2026-07-06": {"groupAverage": 7010, "groupMax": 7050},
                 "2026-08-17": {"groupAverage": 7120, "groupMax": 7180}},
    "enduranceScoreDTO": {"userProfilePK": 1, "calendarDate": DAY, "overallScore": 7250, "classification": 4,
                          "primaryTrainingDevice": True},
}

HILL_SCORE = {
    "userProfilePK": 1, "startDate": "2026-07-09", "endDate": DAY,
    "hillScoreDTOList": [
        {"calendarDate": "2026-09-29", "overallScore": 68, "strengthScore": 60, "enduranceScore": 66,
         "hillScoreClassificationId": 3},
        {"calendarDate": "2026-07-09", "overallScore": 62, "strengthScore": 55, "enduranceScore": 60,
         "hillScoreClassificationId": 3},
        {"calendarDate": DAY, "overallScore": None},
    ],
}

MAX_METRICS_RANGE = [
    {"generic": {"calendarDate": "2026-07-10", "vo2MaxPreciseValue": 52.6, "vo2MaxValue": 53.0}, "cycling": None},
    {"generic": {"calendarDate": "2026-07-12", "vo2MaxPreciseValue": 52.8, "vo2MaxValue": 53.0},
     "cycling": {"calendarDate": "2026-07-12", "vo2MaxPreciseValue": 54.9, "vo2MaxValue": 55.0}},
    {"generic": {"calendarDate": "2026-09-22", "vo2MaxPreciseValue": 53.1, "vo2MaxValue": 53.0},
     "cycling": {"calendarDate": "2026-09-20", "vo2MaxPreciseValue": 55.1, "vo2MaxValue": 55.0}},
    {"generic": {"calendarDate": "2026-09-28", "vo2MaxPreciseValue": 53.4, "vo2MaxValue": 53.0}, "cycling": None},
]

PERSONAL_RECORDS = [
    {"id": 14, "typeId": 12, "activityId": None, "activityType": None, "value": 31245.0,
     "prStartTimeLocalFormatted": "2026-06-01T00:00:00.0"},
    {"id": 12, "typeId": 6, "activityId": 20002, "activityType": "running", "value": 12650.4,
     "prStartTimeLocalFormatted": "2025-10-12T09:00:00.0"},
    {"id": 11, "typeId": 3, "activityId": 20001, "activityName": "Parkrun", "activityType": "running",
     "activityStartDateTimeLocal": "2026-05-02 09:00:00", "value": 1265.0,
     "prStartTimeLocalFormatted": "2026-05-02T09:00:00.0"},
    {"id": 13, "typeId": 7, "activityId": 20003, "activityType": "running", "value": 43120.5,
     "prStartTimeLocalFormatted": "2025-09-14T08:30:00.0"},
]

GEAR_UUID_SHOES = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
GEAR_UUID_BIKE = "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"
GEAR_UUID_OLD = "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"

GEAR = [
    {"gearPk": 501, "uuid": GEAR_UUID_SHOES, "displayName": "Pegasus 40", "customMakeModel": "Nike Pegasus 40",
     "gearMakeName": "Nike", "gearModelName": "Pegasus 40", "gearTypeName": "Shoes", "gearStatusName": "active",
     "dateBegin": "2025-11-01T00:00:00.0", "dateEnd": None, "maximumMeters": 700000.0},
    {"gearPk": 503, "uuid": GEAR_UUID_OLD, "displayName": "Old trail shoes", "gearTypeName": "Shoes",
     "gearStatusName": "retired", "dateBegin": "2023-05-01T00:00:00.0", "dateEnd": "2025-10-30T00:00:00.0",
     "maximumMeters": 800000.0},
    {"gearPk": 502, "uuid": GEAR_UUID_BIKE, "displayName": None, "customMakeModel": "Canyon Endurace",
     "gearTypeName": "Bike", "gearStatusName": "active", "dateBegin": "2024-04-01T00:00:00.0",
     "dateEnd": None, "maximumMeters": 0.0},
]

GEAR_STATS = {  # keyed by gear uuid (FakeGarmin.get_gear_stats looks the uuid up)
    GEAR_UUID_SHOES: {"uuid": GEAR_UUID_SHOES, "totalDistance": 652340.0, "totalActivities": 81, "processing": False},
    GEAR_UUID_BIKE: {"uuid": GEAR_UUID_BIKE, "totalDistance": 4210000.0, "totalActivities": 140, "processing": False},
    GEAR_UUID_OLD: {},
}
