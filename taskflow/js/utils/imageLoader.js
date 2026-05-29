export function loadImage(src) {
    return new Promise((resolve, reject) => {
        const img = new Image();

        img.onload = () => resolve(img);
        img.onerror = reject;

        img.src = src;
    });
}

export function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();

        reader.onload = e => resolve(e.target.result);
        reader.onerror = reject;

        reader.readAsDataURL(file);
    });
}

export async function urlToBase64(url) {
    const res = await fetch(url);
    const blob = await res.blob();
    return await fileToBase64(blob);
}