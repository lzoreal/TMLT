#!/usr/bin/env python3

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


PODCAST_NS = (
    "https://podcastindex.org/namespace/1.0"
)


ET.register_namespace(
    "atom",
    "http://www.w3.org/2005/Atom"
)

ET.register_namespace(
    "itunes",
    "http://www.itunes.com/dtds/podcast-1.0.dtd"
)

ET.register_namespace(
    "podcast",
    PODCAST_NS
)



def main():

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--base-url",
        required=True
    )


    args = parser.parse_args()


    docs = Path(
        "docs"
    )


    source = (
        docs /
        "podcast.xml"
    )


    output = (
        docs /
        "podcast-zh.xml"
    )


    print(
        "Reading:",
        source
    )


    tree = ET.parse(
        source
    )


    root = tree.getroot()


    # --------------------------------------------------------
    # Channel
    # --------------------------------------------------------

    channel = root.find(
        "channel"
    )


    if channel is not None:


        title = channel.find(
            "title"
        )


        if title is not None:

            title.text = (
                "This American Life "
                "中文双语 Transcript Feed"
            )


        language = channel.find(
            "language"
        )


        if language is not None:

            language.text = (
                "zh-CN"
            )


        description = channel.find(
            "description"
        )


        if description is not None:

            description.text = (
                "This American Life "
                "English Chinese bilingual "
                "transcript feed"
            )


        atom_link = channel.find(
            "{http://www.w3.org/2005/Atom}link"
        )


        if atom_link is not None:

            atom_link.set(
                "href",
                args.base_url
                +
                "/podcast-zh.xml"
            )



    # --------------------------------------------------------
    # Items
    # --------------------------------------------------------

    count = 0


    for item in root.findall(
        "item"
    ):


        transcript = item.find(
            f"{{{PODCAST_NS}}}transcript"
        )


        if transcript is None:

            continue



        old_url = transcript.get(
            "url",
            ""
        )


        if "/transcripts/" in old_url:


            episode = (
                old_url
                .split(
                    "/transcripts/"
                )[-1]
            )


            transcript.set(

                "url",

                args.base_url
                +
                "/transcripts/zh/"
                +
                episode

            )


        transcript.set(
            "language",
            "zh-CN"
        )


        count += 1



    print(
        "Updated transcripts:",
        count
    )


    tree.write(
        output,
        encoding="utf-8",
        xml_declaration=True
    )


    print(
        "Generated:",
        output
    )



if __name__ == "__main__":

    main()