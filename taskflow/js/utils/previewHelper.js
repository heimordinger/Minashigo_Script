import {fileToBase64, loadImage, urlToBase64} from "./imageLoader.js";

export async function handleImageFile(node, file) {
    if (!file.type.startsWith("image/")) {
        alert("请拖入图片");
        return;
    }

    const base64 = await fileToBase64(file);

    node.properties.image_base64 = base64;

    node.previewImage = await loadImage(base64);
    node.setDirtyCanvas(true, true);
}

export async function handleImagePath(node, pathOrDataURL) {
    node.previewImage = await loadImage(pathOrDataURL);
    node.setDirtyCanvas(true, true);

    if (pathOrDataURL.startsWith("data:image/")) {
        node.properties.image_base64 = pathOrDataURL;
    } else {
        node.properties.image_base64 = await urlToBase64(pathOrDataURL);
    }
}

export async function restoreImage(node) {
    const base64 = node.properties?.image_base64;

    if (!base64 || node.previewImage) return;

    node.previewImage = await loadImage(base64);
    node.setDirtyCanvas(true, true);
}