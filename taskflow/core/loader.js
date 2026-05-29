// taskflow/core/loader.js
export async function loadAllNodes(){
    await import("../nodes/flow/update_frame.js?t=" + Date.now());
    await import("../nodes/flow/end.js?t=" + Date.now());
    await import("../nodes/flow/start.js?t=" + Date.now());
    await import("../nodes/flow/screenshot.js?t=" + Date.now());
    await import("../nodes/flow/multi_input.js?t=" + Date.now());
    await import("../nodes/flow/label.js?t=" + Date.now());
    await import("../nodes/flow/delay.js?t=" + Date.now());
    await import("../nodes/flow/goto.js?t=" + Date.now());
    await import("../nodes/flow/sleep.js?t=" + Date.now());
    await import("../nodes/flow/wait_image.js?t=" + Date.now());
    await import("../nodes/action/url_goto.js?t=" + Date.now());
    await import("../nodes/action/url.js?t=" + Date.now());
    await import("../nodes/action/click_image.js?t=" + Date.now());
    await import("../nodes/action/match_image.js?t=" + Date.now());
    await import("../nodes/action/click_text.js?t=" + Date.now());
    await import("../nodes/action/dmm_login.js?t=" + Date.now());
    await import("../nodes/action/click.js?t=" + Date.now());
    await import("../nodes/action/click_until_gone.js?t=" + Date.now());
    await import("../nodes/mnsg/scene_detect.js?t=" + Date.now());
    await import("../nodes/test/test_error.js?t=" + Date.now());
}