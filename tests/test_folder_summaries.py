"""Folder summaries include all descendants using the video's label semantics."""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from video_store import VideoStore


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setattr(api_main, "DIRECTORY_PROJECT_ROOT", tmp_path / "application")
    store = VideoStore(tmp_path / "library.db")
    root = store.create_folder("Course")
    child = store.create_folder("Chapter", root)
    deep = store.create_folder("Section", child)
    empty = store.create_folder("Empty", root)
    other = store.create_folder("Other")
    videos = []
    markers = []
    for index, (folder, levels) in enumerate([
        (child, ["standard", "standard", "important", "critical"]),
        (deep, ["standard", "critical", "critical"]),
        (other, ["important", "important", "important"]),
    ]):
        video = store.create_video(str(index), f"{index}.mp4", f"{index}.mp4", "audio.wav", 10000)
        store.set_video_folder(video, folder)
        videos.append(video)
        group = []
        for position, level in enumerate(levels):
            marker = store.add_marker(video, position * 1000, position * 1000 + 500,
                                      "point", "", "", str(position), .9,
                                      "medium_recall" if position % 2 else "high_recall")
            store.set_marker_importance(marker, level)
            group.append(marker)
        markers.append(group)
    store.set_marker_review_status(markers[0][1], "mastered")
    store.set_marker_review_status(markers[0][3], "excluded")
    with TestClient(api_main.create_app(store)) as client:
        yield store, client, (root, child, deep, empty, other), videos, markers


def assert_counts(folder, videos, standard, important, critical):
    assert folder["video_count"] == videos
    assert folder["marker_importance_counts"] == {
        "standard": standard, "important": important, "critical": critical,
    }


def test_unopened_parent_nested_empty_and_sibling_folders_have_correct_totals(tree):
    store, client, (root, child, deep, empty, other), *_ = tree
    top = client.get("/api/library").json()
    folders = {item["folder"]["id"]: item["folder"] for item in top["items"]}
    assert_counts(folders[root], 2, 3, 1, 2)
    assert_counts(folders[other], 1, 0, 3, 0)
    assert store.get_folder(root)["video_count"] == 2
    assert store.list_folder_videos(root) == []  # Listing remains scoped to direct children.
    children = {f["id"]: f for f in client.get("/api/folders", params={"parent_id": root}).json()}
    assert_counts(children[child], 2, 3, 1, 2)
    assert_counts(children[empty], 0, 0, 0, 0)
    assert_counts(client.get(f"/api/folders/{deep}").json(), 1, 1, 0, 2)
    contents = client.get("/api/library", params={"folder_id": deep}).json()
    assert_counts(contents["folder"], 1, 1, 0, 2)
    assert_counts(contents["ancestors"][0], 2, 3, 1, 2)
    assert_counts(contents["ancestors"][1], 2, 3, 1, 2)
    # Every folder's counts equal the sum of its videos' existing badges.
    expected = {"standard": 0, "important": 0, "critical": 0}
    for video in client.get("/api/videos").json():
        if video["folder_id"] in (child, deep):
            for level, count in video["marker_importance_counts"].items():
                expected[level] += count
    assert folders[root]["marker_importance_counts"] == expected


def test_counts_follow_moves_label_changes_exclusion_restoration_and_deletion(tree):
    store, client, (root, child, deep, _empty, other), videos, markers = tree
    assert client.post(f"/api/videos/{videos[1]}/folder", json={"folder_id": other}).status_code == 200
    assert_counts(client.get(f"/api/folders/{root}").json(), 1, 2, 1, 0)
    assert_counts(client.get(f"/api/folders/{deep}").json(), 0, 0, 0, 0)
    assert_counts(client.get(f"/api/folders/{other}").json(), 2, 1, 3, 2)
    store.set_marker_importance(markers[0][0], "critical")
    store.set_marker_review_status(markers[0][2], "excluded")
    assert_counts(client.get(f"/api/folders/{root}").json(), 1, 1, 0, 1)
    store.set_marker_review_status(markers[0][2], "active")
    assert_counts(client.get(f"/api/folders/{child}").json(), 1, 1, 1, 1)
    store.delete_marker(markers[0][0])
    assert_counts(client.get(f"/api/folders/{root}").json(), 1, 1, 1, 0)


def test_direct_videos_and_descendants_are_counted_once_even_with_many_markers(tree):
    store, client, (root, _child, _deep, _empty, _other), videos, _ = tree
    store.set_video_folder(videos[2], root)
    assert_counts(client.get(f"/api/folders/{root}").json(), 3, 3, 4, 2)
