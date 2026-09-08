"""Pure pose geometry tests; no model, devices or cloud credentials."""

import math
import unittest
from dataclasses import replace

from coach.geometry import pose_angles, projected_angle, single_person_angles
from coach.models import Keypoint, PersonPose


def visible_person() -> PersonPose:
    points = [Keypoint(None, None, None) for _ in range(17)]
    for indices, x in (((5, 11, 13, 15), 0.3), ((6, 12, 14, 16), 0.7)):
        for index, y in zip(indices, (0.2, 0.4, 0.6, 0.8)):
            points[index] = Keypoint(x, y, 0.9)
    return PersonPose(0, tuple(points), (0.1, 0.1, 0.9, 0.9), 0.95)


class GeometryTests(unittest.TestCase):
    def test_known_angles(self):
        joint = Keypoint(0.5, 0.5, 1.0)
        left = Keypoint(0.2, 0.5, 1.0)
        for end, expected in (
            (Keypoint(0.8, 0.5, 1.0), 180),
            (Keypoint(0.5, 0.8, 1.0), 90),
            (Keypoint(0.2, 0.2, 1.0), 45),
            (Keypoint(0.1, 0.5, 1.0), 0),
        ):
            with self.subTest(expected=expected):
                self.assertAlmostEqual(
                    projected_angle(left, joint, end, width=100, height=100), expected
                )

    def test_restores_pixel_aspect_ratio(self):
        for width, height in ((640, 360), (1920, 1080), (360, 640), (500, 500)):
            points = [
                Keypoint(x / width, y / height, 0.9)
                for x, y in ((100, 100), (200, 200), (300, 100))
            ]
            self.assertAlmostEqual(projected_angle(*points, width=width, height=height), 90)

    def test_mirror_translation_and_uniform_scaling(self):
        points = [Keypoint(0.1, 0.1, 1), Keypoint(0.4, 0.5, 1), Keypoint(0.6, 0.2, 1)]
        expected = projected_angle(*points, width=1280, height=720)
        transforms = (
            lambda p: replace(p, x=1 - p.x),
            lambda p: replace(p, x=p.x * 0.5 + 0.1, y=p.y * 0.5 + 0.2),
        )
        for transform in transforms:
            self.assertAlmostEqual(
                projected_angle(*(transform(p) for p in points), width=1280, height=720), expected
            )

    def test_rejects_invalid_points_and_dimensions(self):
        valid = Keypoint(0.5, 0.5, 1)
        invalid = (
            Keypoint(None, 0.5, 1),
            Keypoint(math.nan, 0.5, 1),
            Keypoint(0.1, math.inf, 1),
            Keypoint(-0.1, 0.5, 1),
            Keypoint(0.1, 1.1, 1),
            Keypoint(0.1, 0.5, None),
            Keypoint(0.1, 0.5, 0.49),
            Keypoint(0.1, 0.5, math.nan),
            Keypoint(0.1, 0.5, 1.1),
        )
        for point in invalid:
            for index in range(3):
                points = [Keypoint(0.1, 0.2, 1), valid, Keypoint(0.8, 0.2, 1)]
                points[index] = point
                self.assertIsNone(projected_angle(*points, width=640, height=480))
        self.assertIsNone(projected_angle(valid, valid, valid, width=0, height=480))
        self.assertIsNone(projected_angle(valid, valid, valid, width=640, height=-1))

    def test_confidence_boundary_and_degenerate_vectors(self):
        a, joint, b = Keypoint(0.2, 0.5, 0.5), Keypoint(0.5, 0.5, 1), Keypoint(0.5, 0.8, 1)
        self.assertAlmostEqual(projected_angle(a, joint, b, width=640, height=480), 90)
        self.assertIsNone(projected_angle(a, joint, b, width=640, height=480, min_confidence=0.6))
        self.assertIsNone(projected_angle(a, a, b, width=640, height=480))

    def test_coco_left_and_right_are_independent(self):
        person = visible_person()
        self.assertEqual(pose_angles(person, width=640, height=480).values(), (180, 180, 180, 180))
        points = list(person.keypoints)
        points[14] = replace(points[14], confidence=0.1)
        partial = replace(person, keypoints=tuple(points))
        status, angles = single_person_angles((partial,), width=640, height=480)
        self.assertEqual(status, "partial")
        self.assertEqual(angles.values(), (180, None, 180, None))

    def test_multiple_people_do_not_select_a_user(self):
        person = visible_person()
        for people, expected in (((), "no_person"), ((person, person), "multiple_people")):
            status, angles = single_person_angles(people, width=640, height=480)
            self.assertEqual(status, expected)
            self.assertEqual(angles.values(), (None,) * 4)

    def test_missing_keypoints_are_unobservable(self):
        person = replace(visible_person(), keypoints=())
        status, angles = single_person_angles((person,), width=640, height=480)
        self.assertEqual(status, "unobservable")
        self.assertEqual(angles.values(), (None,) * 4)


if __name__ == "__main__":
    unittest.main()
